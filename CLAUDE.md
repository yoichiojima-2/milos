# milos

Secure agent platform on GCP, built to partially qualify for ISO 27001/42001:
client SDK + sandbox runner in one Python package (`src/milos/`), Terraform in
`infra/`, compliance docs in `docs/compliance/`. Firestore is the control
plane, GCS holds session state and evidence bundles, Cloud Run Jobs run
the sandbox.

## Commands

- `uv run pytest tests/ -q` — tests (no GCP needed; fakes in `tests/fakes.py`)
- `uv run ruff check . && uv run ruff format .` — lint/format (CI checks both)

## Architecture notes

- `AgentOptions` (`options.py`) is the serializable subset of
  ClaudeAgentOptions; new serialized fields must be added to
  `_SERIALIZED_FIELDS` or `options_from_doc` rejects them. Options resolve at
  session creation (`agents.resolve`): explicit ← agent ← workspace ←
  `settings/global` ← the floor (model `"sonnet"`, the default-prompt preset);
  the merged result is policy-checked (`policy.check_session`) and snapshotted
  onto the session.
- Workspaces (`workspaces.py`, `workspaces/{name}` in Firestore) are shared
  directories with members: one doc holds stored option defaults AND the
  exclusive lease; members are derived (agents whose stored options name the
  workspace), never stored. A workspace's `CLAUDE.md` sits at its
  shared-directory root and loads as project memory (runner passes
  `setting_sources=["user", "project"]`). The runner claims the workspace
  lease only *after* the pinned-policy load — a session that can't prove its
  rules must not consume the contended lease.
- `layout.py` is the one map of the GCS bucket — every prefix builder lives
  there, nowhere else. Everything a workspace owns nests under
  `workspaces/{name}/`: `ws/` (the shared directory) and `skills/`. Skills
  have two scopes: `skills/` (global, mounted everywhere) and a workspace's
  own (mounted for that workspace, shadowing same-named globals). Checkpoints
  exclude `home/.claude/skills/` — writing them back would resurrect deleted
  skills. The retention lifecycle rule matches only `sessions/`, so
  workspaces and skills never expire by rule.
- **Policies** (`policy.py`, `policies/{vNNNNNN}`) are immutable versions with
  `settings/global.policy_version` as the active pointer — the store has no
  update/delete for them on purpose; the absence of the method is the control.
  Enforcement is exactly two points: session admission (`remote.attach_session`)
  and the gate (`gate.py`, pinned to the session's stamped version by
  `runner._load_pinned_policy`, fail closed). `require_approval` rules return
  hook decision "ask" so the SDK falls through to `can_use_tool` whatever the
  permission_mode.
- The journal (`journal.py`) is a tree (uuid/parent_uuid/branch/seq, doc id ==
  uuid — a crashed runner can never overwrite). `build_context` carries
  `policy_version`/`policy_hash` on every record: that stamp is the evidence
  that rules were in force. Audit rows are committed **before** the tool runs
  (`gate._pre_tool_use`).
- **Evidence** (`evidence.py`) exports JSONL bundles + a hashed manifest to
  the evidence bucket (`infra/evidence.tf`: versioned, lockable
  retention; the runner identity has no grant on it — keep it that way).
  `verify` re-hashes; auditors run it.
- Agents (`agents.py`) carry a `risk` block (required when the policy sets
  `oversight.require_risk_block`) and archive to `revisions/` on every update
  — the AI risk register and version history. Incidents (`incidents.py`) flag
  a session and append an `incident_opened` lifecycle record to its journal.
- Store CRUD blocks are symmetric across `StoreProtocol`, `Store`, and
  `tests/fakes.py::FakeStore` — extend all three together.
- A prompt lands in `sessions/{sid}/inbox` and the runner journals it
  **before** consuming (`runner.run`) — a run that dies in between replays the
  prompt instead of swallowing it. Every exit path releases the session,
  crashes included; a lost lease means write nothing more.
- Compliance mapping lives in `docs/compliance/controls.md` (control → code
  path/Terraform resource → ISO clause); `soa-notes.md` records what milos
  deliberately does not claim. Keep both in sync with control changes.
