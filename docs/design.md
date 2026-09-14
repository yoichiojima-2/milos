# Design

The platform runs business agents that a team shares and that also run unattended. Users authenticate with Google SSO; models are reached only through Vertex AI. This document is the English rendering of the team's system design and describes what the code in `src/milos/` and `infra/` implements.

## 1. Principles

- Three objects: **Agent**, **Session**, **Event**. A session stops for approval and resumes on a human decision. A scheduled run is a session created with its first message. The object model follows Claude Managed Agents so the two stay interchangeable in shape.
- State changes are concentrated in the API. Users, administrators and runners never write to Firestore; publishing a definition is an API call by the admin group.
- The first interface is the CLI. A web UI, diffs and Binary Authorization are later phases.

## 2. Architecture

| Part | Implementation | Responsibility |
| --- | --- | --- |
| API | Cloud Run service. One image deployed as `public` (behind IAP, for users) and `internal` (internal ingress + IAM invoker, for runners, connectors and the scheduler) | Authorization; sessions and events; tool permissions; job launches |
| Runner | Cloud Run Job, one per agent, agent-specific service account | The Agent SDK and bash; polling the API; saving and restoring snapshots |
| Connector | Cloud Run service. One MCP implementation deployed as `internal` (data tools, no NAT, no secrets) and `egress` (SaaS, web fetch; NAT and secrets). Web fetch runs under a separate identity with no secrets | Executing calls the API permitted |
| Scheduler | Cloud Scheduler | Creating scheduled sessions; inspecting expired approvals and stalled runs |
| Firestore | collections in §3 | Definitions, session state, events (the journal) |
| Cloud Storage | `sessions/{id}/snapshots/{n}/` | Transcript and working directory |
| Cloud Logging | locked log bucket in the logging project | Preservation of the audit record |

Supporting services: Vertex AI, Secret Manager (egress only), Artifact Registry, IAP, VPC Service Controls, PAM, Sensitive Data Protection.

## 3. Data model

```
agents/{agent_id}
  versions/{version}
sessions/{session_id}
  events/{event_id}          append-only; seq is the display order
  permissions/{tool_use_id}  create-only; existence means permitted
  approvals/{tool_use_id}    create-only
requests/{key}               create-only; one per (actor, client_request_id)
```

The types are in [`models.py`](../src/milos/models.py). Every document rejects unknown fields. Two fields go beyond the original design and exist because of how the SDK resumes: `Approval.tool_name` and `Approval.args_sha256` let a re-issued tool call be matched by content, and `Permission.approval_tool_use_id` records which approval a permission consumed so an approval is consumed at most once. `Session.consumed_seq` is the runner's replay cursor; `Session.pending` holds the calls parked for approval, with their content hashes, so a decision needs no event lookup.

Vocabulary: the runner sends a *permission request* and the API answers with an *outcome* (`allow`, `deny`, `require_approval`, `stop`); an `allow` creates a *permission*. A `require_approval` parks the session until a person records an *approval* with a *verdict* (`allow`, `deny`).

### Agent

The definition lives in Git and bundles purpose, owner, allowed groups, data classes, allowed tools, approval conditions, model, runner identity, limits and the impact assessment reference. CI validates it (`milos agents validate`) and publishes only validated files as `agents/{id}/versions/{n}`. The registry, the user-facing description and the service-account table are generated from published versions. A definition with missing mandatory fields, no published version, or `enabled: false` is refused on every launch path. `enabled` lives on `agents/{id}`, not on the version: setting it to `false` refuses new sessions and stops running ones at their next permission request or poll.

### Invariants

1. A state change and its event commit in the same transaction; parking a session for approval commits with the `agent.tool_use` event that caused it. Nothing recomputes state from events.
2. `seq` is allocated by the API. Ids deduplicate; `seq` orders.
3. A retried request with the same actor, `client_request_id` and content returns the same result. The record is a create-only `requests/{key}` document, so two retries cannot both succeed; the same key with a different `content_sha256` is rejected as a conflict. Scheduled runs build the key from the Cloud Scheduler job name and schedule time.
4. `permissions/{tool_use_id}` and `approvals/{tool_use_id}` are create-only. An approver equal to the operator is rejected.
5. Runner writes (`agent.*`, `tool.result`, `session.usage`, snapshot pointer) must carry the current `lease.token`.
6. With `Agent.enabled=false` or `Session.status=terminated`, no permission is created.
7. The event stream is the journal. Display events and the model transcript are different things; the transcript is in Cloud Storage.

All seven are enforced in [`service.py`](../src/milos/service.py) and tested in [`tests/test_service.py`](../tests/test_service.py). Who may call what is decided in [`access.py`](../src/milos/access.py) and applied by [`api.py`](../src/milos/api.py).

## 4. Execution contract

1. **Start.** `POST /v1/sessions`. The API checks the definition's version, `enabled` and concurrency, writes the session and its first event, issues a session token and a lease token, and launches the agent's job. The session token is an HMAC over the session id, verified on every internal call and never stored.
2. **Tool call.** The runner's `PreToolUse` hook asks the API (`POST /internal/sessions/{id}/permissions`). The API checks the lease, `enabled` and the allowed tools, commits `agent.tool_use` (and, when a person must decide, the park) in one transaction, writes the audit entry synchronously, and only then creates the permission and answers `allow`; the other outcomes are `require_approval`, `deny` and `stop`. Connectors look the permission up with the API before executing.
3. **Approval.** The session becomes `idle` / `requires_action` with `approval_expires_at`. The runner interrupts the turn, writes a snapshot and exits. An approver (not the operator; identity from IAP) records a verdict (`POST /v1/sessions/{id}/approvals`, event `user.approval`); the API restarts the job. The re-issued call, matched by tool name and argument hash, consumes the approval once; a call with different arguments needs a new approval. Expiry is recorded by inspection as a `deny` verdict with `timed_out: true`, and the job is restarted so the model learns the denial.
4. **Stop.** `user.interrupt` stops the running turn. `POST /v1/sessions/{id}/terminate` sets `terminated` and records `session.status`. `enabled=false` and `terminated` answer `stop` to every later permission request and poll.
5. **Scheduled.** Cloud Scheduler calls `POST /internal/sessions` with its identity token; the API accepts only the scheduler's service account for that route and for inspection.
6. **Administration.** `POST /v1/agents` publishes a validated definition and `PATCH /v1/agents/{id}` enables or disables an agent; both need membership of the admin group. CI publishes the merged definitions this way.

- A run silent for 60 seconds becomes `rescheduling` and inspection restarts it with a new lease. The old job can no longer write. A restart that stalls again becomes `needs_attention`.
- Job retries are zero, so an execution with side effects is never duplicated by the platform.
- A `tool.result` with outcome `unknown` is never retried automatically; exactly-once across external services is not assumed.
- Snapshots are written as `n+1`; the pointer advances afterwards. A failed upload leaves `n` intact.
- `user.message` is processed in `seq` order; progress is read with `GET /v1/sessions/{id}/events?after=`, never through a connection to a particular container.
- Limits: the runner passes the definition's `max_turns` and `max_budget_usd` to the SDK; when reached the session stops with `budget_reached`. Usage is recorded as `session.usage` and never used for the decision. `max_budget_usd` is an estimate-based stop, not a billing cap; monthly spend is watched by a Cloud Billing budget.

## 5. Bash and isolation

- Bash runs inside the runner container. The runner identity has exactly three reaches: Vertex AI (`roles/aiplatform.user`), the internal API (session token), and the snapshot bucket. Bash can reach nothing else, so allowing it does not widen what the agent can do.
- The isolation rests on IAM minimisation. Bash can read the session token and the transcript; it cannot approve. Extra grants to a runner identity are what the service-account reconciliation (REQ-D-08) is for.
- Cloud Run Jobs run on the second-generation execution environment (microVM). Cloud Run sandboxes (Preview) are not used: pre-GA offerings sit outside the data-processing terms.
- The SDK's own tools stay enabled and `PreToolUse` sends every call to the API. `WebFetch` and `WebSearch` are disallowed; the web goes through a connector. `setting_sources=[]` keeps repository settings, hooks and skills out; the definition's system prompt is the only instruction.
- Packages come from Artifact Registry remote repositories (PyPI, npm) over the restricted VIP. Common packages are baked into the image. Fetched packages are not inspected.

## 6. Security boundary

All projects sit under the department folder with Google-managed encryption.

| Project | Holds |
| --- | --- |
| runtime | API, jobs, scheduler, Firestore, snapshot bucket, internal connector |
| logging | locked log bucket, lien |
| data (per sensitivity domain) | classified data |
| egress | egress connector, web fetch, secrets, Cloud NAT. Credentials are separated per environment |

**Network.** One VPC Service Controls perimeter around everything, in dry-run first. VPC-SC does not stop the public internet, so Cloud Run uses Direct VPC egress with `all-traffic` and the runtime project has no NAT (`run.allowedVPCEgress=all-traffic` at the folder). Google APIs resolve to `restricted.googleapis.com` (199.36.153.4/30) through private DNS; `run.app` and `pkg.dev` point there too. The egress project's NAT serves SaaS hosts by FQDN and web fetch on public 443 only; FQDN rules are DNS-based, so the connector also verifies host, TLS and redirects. Web fetch is GET only, rejects redirects to private or metadata addresses, and bounds URL length, hop count and response size.

**Identity.** Users: Google SSO with MFA enforced in Workspace; the API verifies the IAP assertion and ignores identity in bodies. Firestore Security Rules do not apply to server libraries, so authorization is the API's and identities are separated:

| Identity | May |
| --- | --- |
| API | Firestore; run the registered jobs with overrides; write logs; read the token key |
| runner (per agent) | Vertex AI; the snapshot bucket; the internal API; package remotes |
| scheduler | invoke the internal API; its identity is verified on the routes it uses |
| admin group (people and CI) | publish, enable and disable definitions through the public API |
| internal connector | read approved data; no NAT, no secrets |
| egress connector | the SaaS secrets; no data |
| web fetch | nothing |

External credentials live only in Secret Manager. Human administration is time-bound through PAM; no standing write roles.

**Classification and audit.** Masked data is the default; raw data stays inside its sensitivity domain. The API writes an audit entry synchronously (`entries.write`) before permitting a tool; entries carry hashes and short summaries, never raw arguments or output. Cloud Audit Logs for the folder are collected by an aggregated sink into the locked bucket with a project lien. Log-based alerts notify the definition's owner of denial bursts; privilege drift, cost spikes and missing audit entries are operator procedures until their detectors are added.

## 7. Build and verify

Terraform modules: `foundation` / `network` / `runtime` / `egress` / `logging` / `data` / `perimeter`, wired per environment under `infra/envs/`. CI authenticates with Workload Identity Federation, plans on pull requests and applies from `main`. The application (API, runner, models), the connectors and the infrastructure are three areas; the first two develop without GCP credentials on fakes and the Firestore emulator.

### Acceptance

| Subject | Passes when | Where |
| --- | --- | --- |
| Journal (REQ-D-09) | No tool runs before its event and audit entry are committed; the park commits with its event | `test_allowed_tool_is_journaled_audited_then_permitted`, `test_audit_failure_means_no_permission`, `test_permit_commits_request_and_park_together` |
| Stop (REQ-D-18) | After `enabled=false` or `terminate`, every permission request is refused | `test_terminate_denies_every_further_permission`, `test_disabled_agent_stops_running_sessions` |
| Approval (REQ-D-17) | Expiry is recorded as `deny`; changed content needs a new approval | `test_expired_approval_is_recorded_as_timed_out_deny`, `test_changed_arguments_need_a_new_approval` |
| Definition (REQ-D-12) | A definition with a missing mandatory field cannot start a session | `test_invalid_definitions_are_rejected`, `test_create_session_rejects_missing_definition` |
| Limits (REQ-D-19) | The SDK stops at the limit and the session is `budget_reached`; a definition without limits is not published | `test_budget_reached_is_reported`, `test_invalid_definitions_are_rejected` |
| Lease | A stale lease token is rejected; a stalled job is restarted by inspection | `test_stale_lease_is_rejected`, `test_stalled_run_is_rescheduled_once_then_needs_attention` |
| Snapshot | A failed upload leaves the previous snapshot | `test_snapshot_pointer_advances_in_order` |
| Unattended | A scheduled session runs to `idle` and resumes from its snapshot after a CLI approval; only the scheduler identity may schedule | `test_scheduler_creates_idempotent_sessions`, `test_approval_parks_then_resumes_and_executes`, `test_scheduler_identity_is_verified` |
| Administration | Publishing needs the admin group; nobody else can change what runs | `test_publish_requires_admin_group` |
| Network (REQ-D-02, D-20) | `curl` to a public host fails inside the runner; `egress` reaches allowed FQDNs only; `internal` has no NAT | real hardware, see operations |

### Rollout

1. Confirm on real hardware: the `PreToolUse` hook against the API, resume after approval, synchronous log writes, reaching the internal `run.app` address from the VPC, and the connector's identity-token lifetime.
2. With non-sensitive data, exercise request, progress, follow-up, stop, approval and resume.
3. Add failure drills and one SaaS operation; measure latency, idle cost and poll volume.
4. Fix classification inheritance, region, retention, masking and the rules for sending search terms and URLs outward; run the impact assessment; connect a business domain.
