# Control-to-clause mapping

What milos enforces, where each control lives (code path, Terraform resource,
or documented procedure), and which ISO 27001:2022 Annex A control and ISO/IEC
42001:2023 clause it supports. This file is itself a documentation control:
it is versioned in git, and the honest scope statement — what milos does *not*
claim — is in [soa-notes.md](soa-notes.md).

"Partially qualifying" means exactly this: milos implements and evidences the
technical controls below. An ISO certification also needs the management
system around them (scope statements, risk treatment, internal audit,
management review) — that is the operating organization's work, for which
these controls and the evidence exports are inputs.

| id | control | mechanism | ISO 27001 Annex A | ISO 42001 |
|---|---|---|---|---|
| MC-01 | Every tool call is written to the journal **before** it executes; the record carries the call hash, decision, and the policy version in force | `gate.Gate._pre_tool_use` (`src/milos/gate.py`); enforced in code, not by prompt | A.8.15 logging, A.5.28 evidence collection | 8.4, 9.1 |
| MC-02 | Org policy as code: versioned, immutable, enforced at session admission and at the gate; no update/delete API exists for versions | `policy.py`, `store.create_policy` (transactional version alloc), `remote.attach_session`, `runner._load_pinned_policy` | A.5.1 policies for infosec, A.8.2 privileged access | 6.1.2, 8.2 |
| MC-03 | Sessions cannot start without an active policy; a session whose pinned policy cannot be read fails closed (`policy_error`) | `remote.require_active_policy`, `runner._load_pinned_policy` | A.5.1 | 8.2 |
| MC-04 | Human approval gate: policy `require_approval` rules force named tool patterns through a human decision whatever the run's permission mode; timeout = deny, recorded | `gate.Gate.can_use_tool`; approvals collection + journal mirror records | A.5.3 segregation of duties, A.8.2 | 42001 human-oversight expectations (B.9.2) |
| MC-05 | Kill switch: an operator can disable a session; checked on every tool call, every lease renewal, and the inbox poll | `store.is_dead`, `milos kill` | A.5.25 incident response | 8.4 |
| MC-06 | Journal records cannot be overwritten: doc id == record uuid; a crashed runner re-issuing a sequence number forks, never rewrites | `journal.py`, `store.append_event` | A.8.15 | 9.1 |
| MC-07 | Evidence export: hashed, auditor-verifiable bundles (sessions, journal, approvals, policies, agents+revisions, incidents) with a manifest and `milos evidence verify` | `evidence.py`; `milos evidence export/verify` | A.5.28, A.5.31 legal/regulatory | 9.1, 9.2 |
| MC-08 | Evidence immutability: the evidence bucket is versioned, CMEK-encrypted, and under a (lockable) retention policy; the sandbox identity has **no grant** on it | `infra/evidence.tf`; the *absence* of an IAM member in `infra/main.tf` | A.8.15, A.5.33 protection of records | 9.1 |
| MC-09 | CMEK on every store: Firestore and both buckets encrypt under customer-managed keys with automatic rotation | `infra/kms.tf`, `cmek_config`/`encryption` blocks | A.8.24 cryptography | — |
| MC-10 | Data residency: Firestore, buckets, KMS, and the sandbox all pin to one configured region | `var.region`/`var.firestore_location` used on every resource | A.5.31 | — |
| MC-11 | Retention & deletion: session state expires by bucket lifecycle rule; Firestore sessions by `milos sessions purge --older-than N`; the schedule is mirrored declaratively in the policy's `retention` block | `infra/main.tf` lifecycle rules; `cli._sessions` purge | A.8.10 information deletion | 8.3 |
| MC-12 | Access logging: Data Access audit logs on Firestore/GCS/KMS/Run, routed to a dedicated logging bucket with its own retention — who read what, independent of app code | `infra/logging.tf` | A.8.15, A.8.16 monitoring | 9.1 |
| MC-13 | Least-privilege sandbox: the runner identity holds Vertex, Firestore, the state bucket, and self-retrigger on its own job — no secrets by default, no evidence-bucket access, `anthropic-api-key` readable only under `model_backend = "anthropic"` | `infra/main.tf` IAM members | A.8.2, A.5.15 access control | 8.2 |
| MC-14 | Model-traffic boundary: Vertex by default (traffic stays in the project); the Anthropic API path is a double opt-in — the Terraform `model_backend` variable AND the policy's `model_backends` list | `options.model_env`, `policy.check_session`, `infra/variables.tf` | A.8.23 web filtering (in spirit), A.5.14 information transfer | 8.2 |
| MC-15 | Network egress control (opt-in): default-deny VPC, FQDN allowlist, Private Google Access | `infra/network.tf` (`egress_control = true`) | A.8.23 | 8.2 |
| MC-16 | AI risk register: every agent carries `{purpose, impact, owner, review_by}` when the policy requires it; `milos agents` flags overdue reviews | `policy.validate_risk`, `agents._check_risk`, `cli._agents` | A.5.9 inventory (of AI assets) | 6.1.2, 6.1.4 AI risk assessment |
| MC-17 | Model & prompt versioning: the merged options (model, system prompt, budgets) are snapshotted onto every session at creation; every journal record's context carries model + milos version + policy hash; agent edits archive the prior doc to `revisions/` | `remote.attach_session`, `journal.build_context`, `agents.update` | A.8.32 change management | 42001 versioning expectations (B.6.2.5) |
| MC-18 | AI incident log: incidents open against a session with reason/severity, appear inline in the transcript, close with a resolution, and export in evidence | `incidents.py`; `milos incidents` | A.5.24–A.5.27 incident management | 42001 incident clauses (10.2, B.6.2.8) |
| MC-19 | Human-oversight records: every approval decision stores who decided (person via CLI, `sdk` callback, or `timeout`) and when, in the queue and mirrored into the journal | `store.decide_approval`, `gate.can_use_tool`, `remote._relay_approvals` | A.5.3 | B.9.2 |
| MC-20 | Backups: Firestore point-in-time recovery + weekly backups (14-day retention); delete protection on the database | `infra/main.tf` | A.8.13 information backup | — |
| MC-21 | Workspace concurrency control: a shared directory admits one live run at a time via a transactional lease; a contender fails fast with a journaled `workspace_busy` record (evidence of the refusal), and the lease is claimed only after the pinned policy loads — an unprovable session never consumes it | `store.claim_workspace`, `runner.run` | A.5.15 access control, A.8.15 | 8.4 |
| MC-22 | Skills-mount integrity: skills restore from the shared `skills/` prefixes into each run's HOME and are excluded from the home checkpoint, so a session can never persist edits to (or resurrect deletions of) the skills every other session mounts | `runner.run` (checkpoint exclude), `state.checkpoint` | A.8.32 change management | 8.2 |
| MC-23 | Retention scoping: the state-bucket lifecycle rule expires `sessions/` only — shared workspaces and skills are durable content and never deleted by schedule | `infra/main.tf` lifecycle rules (matches_prefix) | A.8.10 | 8.3 |

## Operating procedures (documentation controls)

These are procedures the operating organization runs; milos makes them
one-command each:

- **Monthly evidence export** — `milos evidence export --from <first> --to
  <last>`; the locked bucket then holds the record. Continuous mirroring is
  deliberately not built (see soa-notes.md).
- **Retention run** — `milos sessions purge --older-than <policy days>`
  scheduled alongside the bucket lifecycle rules (which need no scheduling).
- **Agent review** — `milos agents` lists `REVIEW OVERDUE` entries; the owner
  named in the risk block re-assesses and updates (which archives a revision).
- **Production checklist** — set `lock_evidence_retention = true`, decide
  `egress_control`, keep `model_backends: [vertex]` unless the Anthropic path
  was consciously accepted, and scope project IAM as you would scope the
  trust boundary itself.
