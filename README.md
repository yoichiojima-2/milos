# milos

Run [`claude_agent_sdk`](https://code.claude.com/docs/en/agent-sdk) agents in
sandboxed Cloud Run Jobs inside your own GCP project — designed from the start
to partially qualify for **ISO 27001** (information security) and **ISO 42001**
(AI management). Same API shape as the SDK; models go through Vertex AI by
default, every session runs under a versioned org **policy**, every tool call
is written to an audit trail *before* it executes, calls can be gated on human
approval, and the whole record exports as a hashed, auditor-verifiable
**evidence bundle**.

The idea is to add as little as possible on top of what already exists.
`claude_agent_sdk` provides the harness (loop, tools, permissions, sessions,
resume); GCP managed services cover the control plane and the compliance
substrate: Firestore holds session state and the journal, GCS (CMEK, versioned,
retention-locked) holds workspaces and evidence, Cloud Run Jobs are the
sandbox, KMS is the key custody, Cloud Audit Logs are the who-read-what layer,
IAM is auth. milos is one Python package and one Terraform module — no servers,
no REST API, roughly zero always-on cost.

## Use

```python
from milos import query, AgentOptions, PermissionResultAllow

async def approve(tool_name, tool_input, context):
    print(f"allow {tool_name}({tool_input})?")
    return PermissionResultAllow()

async for message in query(
    prompt="profile the CSVs in the working directory and write a report",
    options=AgentOptions(
        model="claude-sonnet-5",
        system_prompt="You are a careful data analyst.",
        allowed_tools=["Read", "Write", "Bash", "Glob", "Grep"],
        permission_mode="default",   # unlisted tools pause for approval
        can_use_tool=approve,        # drives the approval queue from your machine
    ),
):
    print(message)
```

The harness runs in the project's Cloud Run Job (project from
`AgentOptions.project` or `$MILOS_PROJECT`); message types stream back through
Firestore. Sessions are durable: `AgentOptions(resume="sess_...")` reconnects,
idle sessions scale to zero. Multi-turn mirrors `ClaudeSDKClient`:

```python
from milos import MilosClient, AgentOptions

async with MilosClient(AgentOptions()) as client:
    await client.query("read the data")
    async for message in client.receive_response():
        ...
    await client.query("now summarize it")     # same durable session
    async for message in client.receive_response():
        ...
```

Ops without a UI:

```
milos run "collect today's numbers" --agent analyst
milos sessions                       # recent sessions: status, cost, policy version
milos tail sess_...                  # follow a session's journal (messages + audit)
milos rewind sess_... <event_uuid>   # branch the transcript from a past event
milos approvals                      # pending approvals, across every session
milos approvals sess_... allow <call_hash>
milos kill sess_...                  # kill switch: denies every further tool call
```

## Policy as code

One org policy governs every session. It is YAML, applied as an immutable
version, and enforced twice in code — at session admission (disallowed
model/backend/mode/tool rejected, budgets clamped) and inside the sandbox's
gate on every tool call. **No policy applied → sessions refuse to start.**

```
milos policies apply policy.yaml     # validate, store vN+1, activate
milos policies show                  # the active version
milos policies diff v000001 v000002
```

```yaml
models: { allow: ["claude-sonnet-*"] }
model_backends: [vertex]             # omit "anthropic" to ban API-key egress
permission_modes: { deny: [bypassPermissions] }
tools: { deny: [WebSearch] }
require_approval:
  - tools: ["Bash", "mcp__*"]        # human sign-off even under acceptEdits
    reason: "shell and MCP calls need approval"
budgets: { max_budget_usd: 20.0, max_turns: 200 }
oversight: { require_risk_block: true }
```

The admitted version (and its content hash) is stamped onto the session and
into every journal record's context, and the runner pins its gate to that exact
version — what the evidence says was in force is what ran, resumes included.
Versions have no update or delete API; changing the rules is applying the next
version. A permissive starter lives at
[docs/compliance/policy.example.yaml](docs/compliance/policy.example.yaml).

## Evidence

```
milos evidence export --from 2026-08-01 --to 2026-09-01
milos evidence verify exp_20260901_ab12cd34
```

Export writes one bundle to the evidence bucket — sessions (with their options
and policy stamps), the full journal (messages, the tool-call audit trail,
approvals, lifecycle), the approval queue, every policy version, agents with
their risk blocks and revision history, and incidents — each file sha256'd
into a manifest with one bundle hash. `verify` re-computes everything; an
auditor can run it with read access alone.

The evidence bucket is versioned, CMEK-encrypted, and under a retention policy
(lockable: `lock_evidence_retention = true` makes bundles undeletable by
anyone, project owners included, until retention expires). The sandbox
identity holds no grant on it at all. Firestore is the working copy; the
locked bucket is the record — export monthly.

The full control-to-clause mapping (ISO 27001 Annex A / ISO 42001) is in
[docs/compliance/controls.md](docs/compliance/controls.md); what milos
deliberately does *not* claim is in
[docs/compliance/soa-notes.md](docs/compliance/soa-notes.md).

## AI oversight (ISO 42001)

- **Risk register** — with `oversight.require_risk_block`, every stored agent
  carries `{purpose, impact, owner, review_by}`; `milos agents` flags overdue
  reviews. The agents collection *is* the register.
- **Version history** — every `agents update` archives the prior definition to
  `revisions/`; every session snapshots its merged options at creation, so
  which model and prompt produced which output is always answerable.
- **Incidents** — `milos incidents open sess_... --reason "..." --severity
  high` flags a session (the transcript shows it inline), `close` records the
  resolution, and both export in evidence.
- **Human oversight records** — every approval stores who decided and when
  (CLI user, SDK callback, or the fail-closed timeout).

```
milos agents create analyst --system-prompt "..." --allow Read --allow Bash \
    --risk-purpose "weekly metrics digest" --risk-impact low \
    --risk-owner you@example.com --risk-review-by 2027-01-01
milos agents revisions analyst
milos incidents open sess_... --reason "asked for credentials" --severity high
```

## Agents

An agent is a named, stored run configuration — the persona a session runs as.
Reference it with `AgentOptions(agent=...)`: stored options become defaults,
explicit fields override per field. Options resolve at session creation
(explicit ← agent ← workspace ← `settings/global` ← the built-in floor: model
`"sonnet"`, the harness's default prompt) and the merged result is snapshotted
onto the session, then checked against the policy.

## Workspaces

A workspace is a shared directory with members: one GCS-backed working
directory (`workspaces/{name}/ws/`, exclusive lease — one live run at a time,
a contender fails fast with `stop_reason="workspace_busy"` and its prompt
stays queued), a `CLAUDE.md` at its root loaded as project memory for every
run under the workspace, the workspace's own skills, and stored option
defaults inherited by every session that names it. Members are derived, not
stored: the agents whose saved options name the workspace.

```
milos workspaces create dev --model claude-sonnet-5
milos workspaces claude-md dev --file CLAUDE.md   # print without --file
milos workspaces                                  # busy/free, members, holder
```

From the SDK: `AgentOptions(workspace="dev")`. The runner claims the workspace
lease only after it has loaded the session's pinned policy — a session that
cannot prove its rules never consumes the contended resource.

## Skills

Sessions carry [Agent Skills](https://code.claude.com/docs/en/skills) — a
skill is a directory (`SKILL.md` plus resources) under `skills/{name}/` in
the state bucket, restored into every run's `~/.claude/skills/` at start.
Two scopes: global (`skills/`, mounted everywhere) and per-workspace
(`workspaces/{name}/skills/`, mounted for that workspace's sessions,
shadowing same-named globals). The prefix is the single source of truth:
checkpoints never write skills back, so a deleted skill stays deleted.

```
milos skills                          # list skills with descriptions
milos skills push ./skills/*          # upload local skill directories
milos skills files pdf                # one skill's files
milos skills cat pdf SKILL.md         # print one skill file
milos skills sync                     # seed from the official anthropics/skills repo
```

`push` names the skill after its directory (`--name` overrides), requires a
real `SKILL.md` at the root, skips symlinks/oversized files/tooling state,
merges by default, and prunes with `--replace`. `sync` runs on the operator's
machine (it fetches github.com), never in the sandbox — the egress allowlist
is unaffected.

## Security model

- **Data boundary** — model calls exit only via Vertex AI by default (the
  sandbox has no Anthropic API key); state lives in your project's
  Firestore/GCS under customer-managed keys, pinned to one region.
  `model_backend = "anthropic"` is a double opt-in: the Terraform variable
  mounts the key *and* the policy's `model_backends` must allow it.
- **Credential-less sandbox** — the runner's service account holds exactly:
  `aiplatform.user`, `datastore.user`, object access on the state bucket, and
  self-retrigger on its own job. No secrets by default, and **no access to the
  evidence bucket** — evidence integrity rests on IAM, not convention.
- **Audit before execution** — a `PreToolUse` hook appends the audit record to
  the journal and awaits the commit *before* the tool runs; the gate is
  enforced in code, never by prompt. Policy denials and forced approvals
  happen at the same point.
- **Approvals fail closed** — gated calls block until decided; timeout is an
  explicit recorded deny.
- **Kill switch** — `milos kill` flips `disabled`; checked on every tool call.
- **Access logging** — Data Access audit logs on Firestore/GCS/KMS route to a
  retained logging bucket: who read what, independent of application code.
- **Network egress** — `terraform apply -var egress_control=true` puts the
  sandbox behind a default-deny VPC with an FQDN allowlist and Private Google
  Access (see the honest DNS-based-FQDN caveat in soa-notes.md).
- **IAM is the tenancy model** — one GCP project = one trust boundary.

## Deploy

From the repo root:

```sh
# 0. Set once; every step below reads these. MILOS_PROJECT is also the env var
#    the client and CLI read at runtime.
export MILOS_PROJECT=your-project
export MILOS_IMAGE=asia-northeast1-docker.pkg.dev/$MILOS_PROJECT/milos/runner:latest

# 1. Infrastructure (KMS, Firestore, buckets, audit logs, job, service account)
gcloud storage buckets create gs://$MILOS_PROJECT-tfstate \
  --project $MILOS_PROJECT --location asia-northeast1
gcloud storage buckets update gs://$MILOS_PROJECT-tfstate --versioning
terraform -chdir=infra init -backend-config="bucket=$MILOS_PROJECT-tfstate"
terraform -chdir=infra apply -var project=$MILOS_PROJECT -var image=$MILOS_IMAGE

# 2. Runner image
gcloud builds submit --project $MILOS_PROJECT --tag $MILOS_IMAGE .

# 3. Apply a policy (nothing runs without one), then smoke test
milos policies apply docs/compliance/policy.example.yaml
milos run "hello"
```

A fresh project applies twice: the first `terraform apply` creates Artifact
Registry (and fails on the job until an image exists), then step 2 pushes,
then re-apply. Production checklist: `lock_evidence_retention = true`, decide
`egress_control`, and scope callers' IAM (`datastore.user`,
`run.jobsExecutorWithOverrides` on the runner job, read on the state bucket)
the way you'd scope the project itself.

## Out of scope

Workflows/cron, artifact spaces, connectors, a web console, BigQuery
analytics, evidence signing, continuous journal mirroring — each either
inherited later from the same architecture (see
[syros](https://github.com/yoichiojima-2/syros), milos's feature-richer
sibling) or named as future work in the compliance notes.

## Development

```sh
uv sync --group dev
uv run pytest -q                                     # no GCP needed; fakes in tests/fakes.py
uv run ruff check . && uv run ruff format --check .
```

Layout: `src/milos/` (client SDK + sandbox runner in one package), `infra/`
(Terraform), `docs/compliance/` (control mapping, SoA notes, example policy
and risk block), `tests/`.

## Status

Early and evolving — interfaces may still change between releases.
