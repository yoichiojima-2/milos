# What milos does not claim

The honest half of [requirements.md](requirements.md). Decided, not forgotten.

- **Snapshot access is bucket-wide per runner identity.** The design calls for a per-session downscoped token; today each agent's runner identity holds `objectUser` on the whole snapshot bucket, so a prompt-injected agent could read another session's snapshot of the same or another agent. Downscoped credentials issued by the API at launch are the next step; until then the bucket is treated as one classification.
- **Resume after approval relies on the model re-issuing the call.** The runner exits when a call needs a person; on resume it sends a continuation message and the API matches the re-issued call by tool name and argument hash. If the model issues a different call it needs a new approval, which is safe; if the SDK does not resume from the snapshot's session id on real hardware, the fallback is to keep the runner alive during the approval window.
- **A permission can be used more than once within its lease.** Permissions are create-only and the connector looks them up by content; a second identical call inside the same execution is not distinguished. The window is one execution, and the journal shows every request.
- **FQDN egress rules are DNS-based.** Hosts sharing an allowed address (a CDN edge) are not separated by the firewall. The connector verifies host, TLS and redirects; SNI-level enforcement needs Secure Web Proxy.
- **Connector identity tokens are fetched at run start.** The SDK's MCP configuration is static for the run; identity tokens last an hour. Runs longer than that must be confirmed on real hardware or bounded by `runner_timeout`.
- **Administration is a group-gated API call, not a PAM-elevated console action.** Publishing, enabling and disabling agents go through `POST /v1/agents` and `PATCH /v1/agents/{id}` for members of the admin group; Cloud Audit Logs record the request and Firestore keeps the version. The approval step for a definition change is the pull request review, not the platform.
- **No masking in code.** Sensitive Data Protection belongs to the data projects' ingestion pipelines, which this repository does not build. Until it exists, only masked or non-sensitive data may be connected.
- **Cloud Run sandboxes are not used.** They are pre-GA and outside the data-processing terms. Isolation is the second-generation execution environment plus IAM.
- **No customer-managed keys.** Google-managed encryption everywhere. Key custody added operational risk without changing the trust model at this deployment's level.
- **VPC Service Controls starts in dry-run.** Nothing is blocked until the violation log is clean and `dry_run` is turned off; until then the network modules are the boundary.
- **Deviation detection is one alert.** A denial-burst alert ships; privilege drift, cost spikes and missing-audit-entry detectors are procedures until their queries are written.
- **Binary Authorization and the evaluation harness are later phases** (REQ-O-07, REQ-O-09).
- **Certification is the organization's.** The platform supplies technical controls and records; the ISMS and AIMS (scope, risk treatment, internal audit, management review) are the operating organization's.
