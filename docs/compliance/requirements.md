# Requirements to implementation

The team's security requirements derive ISO/IEC 27001 and 42001 controls into system design requirements (REQ-D) and dev/ops flow requirements (REQ-O). This table records where each lands in this repository and how it is verified. "Procedure" means an operator activity the platform supports but cannot itself prove.

## System design (REQ-D)

| ID | Requirement | Implementation | Verification |
| --- | --- | --- | --- |
| REQ-D-01 | Data carries a class; the class decides project placement and access boundary | `modules/foundation` project roles; `modules/data` per class with labels and dataset IAM; `AgentVersion.data_classes` → `Session.classification` | `terraform validate` in CI; label query on data projects (procedure) |
| REQ-D-02 | One boundary around every project; no structural way to move data out | `modules/perimeter` (VPC-SC, dry-run first); `modules/network` no NAT, restricted VIP only, default-deny egress; `modules/egress` FQDN rules per identity | Runner `curl` to a public host fails (first deploy §5); VPC-SC dry-run log |
| REQ-D-03 | Sensitive data masked on ingestion; raw data stays in its domain | Not in code. Sensitive Data Protection on the ingestion side of data projects (rollout step 4) | Procedure until built |
| REQ-D-04 | Encryption at rest documented | Google-managed keys everywhere; secrets only in Secret Manager (`modules/runtime`, `modules/egress`) | Document (this file) |
| REQ-D-05 | Retention per class enforced by rule | `modules/data` lifecycle and table expiration; `modules/runtime` snapshot lifecycle | `terraform validate`; rule presence |
| REQ-D-06 | One identity system; no platform-held credentials | IAP on the public API (`iap_enabled`), assertion verified in `auth.IapVerifier`; API keys only in Secret Manager | `test_public_requires_iap`; secret scan (procedure) |
| REQ-D-07 | No standing admin; time-bound elevation | PAM (organization side); Terraform grants no human roles | IAM query for Owner/Editor (procedure) |
| REQ-D-08 | One service account per agent, traceable to the definition | `modules/runtime` `google_service_account.runner[agent]`; `AgentVersion.runner_sa`; `definitions.registry()` | Compare `terraform output runner_service_accounts` with the registry (procedure) |
| REQ-D-09 | Every tool call journaled and audited before it runs; enforced in code | `service.permit`: `agent.tool_use` event committed, `audit.write` synchronous, then `permissions/{id}`; runner `Gate.pre_tool_use` fails closed | `test_allowed_tool_is_journaled_audited_then_permitted`, `test_audit_failure_means_no_permission`, `test_unreachable_api_denies_fail_closed` |
| REQ-D-10 | Audit logs in a dedicated project nobody can alter | `modules/logging`: locked bucket, folder sink with children, lien | `terraform validate`; delete attempt refused (procedure) |
| REQ-D-11 | Detect and notify deviations | `modules/runtime` log metric + alert on denial bursts; owner in the definition | Alert test event (procedure) |
| REQ-D-12 | No agent without a definition | `definitions.load` and the `AgentVersion` validators; `service.create_session` refuses missing or unpublished definitions | `test_invalid_definitions_are_rejected`, `test_create_session_rejects_missing_definition` |
| REQ-D-13 | Register and documents generated from definitions | `milos agents registry` | `test_registry_is_generated_from_published_versions` |
| REQ-D-14 | Intended use in the definition; deviations blocked structurally | `AgentVersion.allowed_tools`, `service._decide` | `test_unlisted_tool_is_denied` |
| REQ-D-15 | Dataset provenance per unit | `modules/data` dataset labels (`source`) | Label query (procedure) |
| REQ-D-16 | Impact assessment, verification and approval before operation | PR review of definitions; CI validation; publishing is a CI credential | Branch protection (REQ-O-02) |
| REQ-D-17 | Irreversible operations need a human; timeout is a recorded deny | `approval_required`; `service.permit` parks the session; `service.confirm` (not the operator); `service.inspect` expires with `timed_out` | `test_approval_flow_parks_session_and_resumes_on_decision`, `test_expired_approval_is_recorded_as_timed_out_deny` |
| REQ-D-18 | Immediate stop that binds every later call | `Agent.enabled`, `terminate`; checked in `permit`, `poll`, `_grant` | `test_terminate_denies_every_further_permission`, `test_disabled_agent_stops_running_sessions`, `test_stop_between_request_and_grant_creates_no_permission` |
| REQ-D-19 | Token and cost limits; automatic stop | `max_turns`, `max_budget_usd` mandatory in the definition and passed to the SDK (`runner.build_options`) | `test_budget_reached_is_reported`; `max_turns: null` rejected |
| REQ-D-20 | Models only through Vertex AI | Runner env `CLAUDE_CODE_USE_VERTEX=1`; no Anthropic key anywhere; egress FQDN list excludes model APIs | `test_plain_turn_reports_messages_and_finishes` asserts the env; FQDN review (procedure) |

## Dev/ops flow (REQ-O)

| ID | Requirement | Implementation | Verification |
| --- | --- | --- | --- |
| REQ-O-01 | All infrastructure as code; no manual path | `infra/` only; drift detection by scheduled `plan` (procedure) | CI `terraform validate` |
| REQ-O-02 | Changes reach `main` through reviewed pull requests | GitHub branch protection (organization side) | Settings check (procedure) |
| REQ-O-03 | dev/stg/prod separated; data projects prod-only | `infra/envs/<env>`; data module instantiated per environment with synthetic data outside prod | Procedure |
| REQ-O-04 | No production data in tests | `tests/` uses fakes and synthetic definitions only | Test review |
| REQ-O-05 | Vulnerability scanning of image and dependencies | Artifact Registry scanning (enable on the repository); dependency updates by PR | Procedure |
| REQ-O-06 | Secret and static checks block merges | CI: ruff, pytest, definition validation | `.github/workflows/ci.yml` |
| REQ-O-07 | Only CI-built, signed images deploy | CI builds; Binary Authorization is a later phase | Not yet |
| REQ-O-08 | Agent changes through the catalogue with approval and an audit trail | Definitions in Git, published as immutable versions; no direct-write path for users or runners | `test_publish_increments_version` |
| REQ-O-09 | Evaluation harness before approval | Not yet | — |
| REQ-O-10 | Backups and restore drills | Firestore PITR + weekly backups; bucket versioning | Restore drill (procedure) |
| REQ-O-11 | Quarterly access review | IAM export (procedure) | Procedure |
| REQ-O-12 | Supplier certification check yearly | Procedure | Procedure |
| REQ-O-13 | Allowed services fixed | `modules/foundation` API lists; `restrictServiceUsage` at the folder (organization side) | Diff enabled APIs against the list (procedure) |
| REQ-O-14 | Admin operations only on audited paths | Cloud Audit Logs to the locked bucket; no standing write roles | Admin Activity log review (procedure) |

## Existing-project development variant

`infra/envs/existing-project` supports a personal project without an organization.
Individual users are authenticated by IAP and must also appear in the immutable
agent definition's `allowed_users`; the existing group check remains available.
The API uses the same membership check for session creation and approvers, and
still rejects self-approval. See `tests/test_api.py` for both denied and permitted
requests. This variant does not implement the cross-project or folder controls
listed above; see `soa-notes.md`.
