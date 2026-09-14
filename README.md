# milos

A secure platform for running [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk) agents on Google Cloud, for a team that shares agents, runs them unattended, and needs to prove afterwards what happened.

Three ideas carry the design:

- **The API owns every state change.** Users and runners never write to Firestore. A state change and the event that describes it commit in one transaction, so the event stream is the journal.
- **Nothing runs without a record.** Every tool call is journaled and written to a locked audit log before a permission exists. Irreversible calls stop the session until a person who is not the operator decides; a timeout is a recorded denial.
- **The sandbox can reach three things.** Vertex AI, the internal API, and its own snapshots. The network has no route to the internet. The web and SaaS are reached through connectors in a separate project that verify each call with the API first.

## Parts

| Part | Runs as | Does |
| --- | --- | --- |
| API | Cloud Run service, one image deployed as `public` (behind IAP) and `internal` (runners, connectors, scheduler) | Authorization, sessions and events, tool permissions, job launches |
| Runner | Cloud Run Job, one per agent, each under its own service account | The Agent SDK with a `PreToolUse` hook that asks the API before every tool call; snapshots to GCS |
| Connector | Cloud Run service, `internal` (data, no NAT, no secrets) and `egress` (SaaS credentials, NAT) | MCP tools that check their permission with the API before acting |
| Scheduler | Cloud Scheduler | Unattended sessions; inspection every minute (expired approvals, stalled runs) |
| Firestore | runtime project | Agents, sessions, events, permissions, approvals |
| Cloud Storage | `sessions/{id}/snapshots/{n}/` | Transcript and working directory |
| Cloud Logging | locked bucket in the logging project | Audit entries and Cloud Audit Logs for the whole folder |

The full design is in [docs/design.md](docs/design.md); how it maps to ISO/IEC 27001 and 42001 requirements is in [docs/compliance/requirements.md](docs/compliance/requirements.md), and what it deliberately does not claim in [docs/compliance/soa-notes.md](docs/compliance/soa-notes.md).

## An agent

An agent is a YAML definition in Git. CI validates it; only validated definitions are published, as immutable versions. The definition names the purpose, the owner, who may start it, which data classes it touches, which tools it may use and which of those need a person, its limits, its model and its runner identity. The registry of what is deployed is generated from the published versions (`milos agents registry`), never written by hand.

```yaml
agent_id: analyst
purpose: Summarise the weekly numbers into a short report.
owner: owner@example.com
allowed_groups: [analysts@example.com]
data_classes: [C1]
allowed_tools: [Read, Glob, Grep, Write, Bash, mcp__egress__web_fetch]
approval_required: [Bash, mcp__egress__web_fetch]
approval_ttl_sec: 3600
max_turns: 40
max_budget_usd: 5.0
max_concurrent_sessions: 2
model: claude-sonnet-5@20260601
runner_sa: milos-runner-analyst@milos-runtime-dev.iam.gserviceaccount.com
connectors: [egress]
system_prompt: |
  You are a careful analyst. Work only inside the working directory.
```

## A session

```sh
milos agents list                       # what you may run, and which tools pause for approval
milos run analyst "Summarise last week's numbers." --approver lead@example.com
milos sessions                          # status, stop reason, pending tool calls
milos events sess_…  --follow           # the journal: messages, tool requests, decisions
milos pending                           # as the approver: waiting calls with their arguments
milos allow sess_… toolu_…              # a person other than the operator decides
milos send sess_… "Also include June."  # a follow-up; an idle session restarts
milos interrupt sess_…                  # stops the current turn
milos terminate sess_…                  # ends the session; every later tool request is refused
```

`run` follows the session (`--detach` returns the id instead). When a followed session stops, the CLI prints the command that moves it on: the `allow`/`deny` pair for a waiting tool call, or `send` for an idle turn. The CLI reads `MILOS_API_URL` and the token settings from `.env` in the working directory; `.env.example` lists them.

A session starts running and stops in one of five ways: `end_turn` (idle, restarts on the next message), `requires_action` (a tool call waits for a person), `budget_reached` (the definition's turn or cost limit), `stopped` (terminated, or the agent was disabled), `needs_attention` (a run died twice; inspection gave up). Disabling an agent stops every session at its next tool request or poll.

## Layout

```
src/milos/
  models.py       Agent, AgentVersion (with the definition rules), Session, Event, Lease, Permission, Approval
  store.py        transactional document store over Firestore (fake in tests/)
  service.py      the execution contract: every state change, every invariant
  api.py          FastAPI; public (IAP) and internal (session + lease tokens) routes
  auth.py         IAP assertions, session tokens, group membership
  audit.py        synchronous Cloud Logging entries
  jobs.py         Cloud Run Job launches
  definitions.py  YAML definitions: loading, publishing, the registry
  runner.py       the job: SDK + PreToolUse hook, poll loop, snapshots
  control.py      the runner's client for the internal API
  snapshots.py    GCS snapshots of transcript and working directory
  connector.py    MCP connectors with the permission check; web_fetch
  client.py       client for the public API
  cli.py          `milos`
agents/           definitions
infra/            Terraform: modules/{foundation,network,runtime,egress,logging,data,perimeter}, envs/dev
docs/             design, operations, compliance
tests/            no GCP needed; fakes.py stands in for every dependency
```

## Development

```sh
uv sync --group dev
uv run pytest -q                                  # no credentials needed
uv run ruff check . && uv run ruff format --check .
uv run mypy                                       # strict on src/milos
uv run milos agents validate agents/*.yaml deployments/*/*.yaml
```

The tests drive the real service through the real API with the SDK replaced by a scripted client (`tests/test_runner.py`), so the approval flow, the lease, the stop signal and the snapshot pointer are exercised end to end in memory. Firestore's transaction semantics that matter (create-only documents, dotted updates, rollback) are mirrored by `tests/fakes.py`; run the same suite against the emulator by setting `FIRESTORE_EMULATOR_HOST` before adding Firestore-specific tests.

A local API without GCP:

```sh
MILOS_DEV_USER=you@example.com MILOS_TOKEN_KEY=dev MILOS_PROJECT=local \
FIRESTORE_EMULATOR_HOST=localhost:8080 uv run milos serve api
```

## Deploy

See [docs/operations.md](docs/operations.md). In short: `terraform apply` in `infra/envs/dev` creates the four projects and everything in them, CI builds the one image, `milos agents publish` puts definitions in Firestore, and the first deploy has a short list of things to confirm on real hardware before data with any classification is connected.

## Status

Rewritten in September 2026 from the platform design in the team's Notion. The code is complete for the design's first deployment step; the items the design itself marks as "confirm on real hardware" are listed in [docs/operations.md](docs/operations.md#first-deploy).
