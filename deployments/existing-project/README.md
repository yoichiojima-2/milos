# Milos development deployment — 2026-09-12

Project: `milos-20260827` (`871659126564`), region: `asia-northeast1`.
Terraform root: `infra/envs/existing-project`; remote state:
`gs://milos-20260827-tfstate/terraform/state/default.tfstate`.

## Deployed

- API services: `milos-api-public`, `milos-api-internal`.
- Connectors: `milos-connector-internal`, `milos-egress-connector`, `milos-egress-web-fetch`.
- Runner job: `milos-runner-analyst`, with its own service account.
- Separate runtime and egress networks, restricted Google API DNS, default-deny runtime egress.
- Fresh snapshot bucket: `milos-20260827-snapshots`; data bucket: `milos-20260827-data`.
- Inspection scheduler: `milos-inspect`.
- Public access is restricted to `yoichiojima@gmail.com` through IAP.
- `analyst` v1 is published **disabled**, pending model access.

Image: `asia-northeast1-docker.pkg.dev/milos-20260827/milos/milos:reset-20260912-3`.
Verified build digest: `sha256:2db906870cbe3e85d5e71a81486d52ed8bcdac2863cdfd346b868a4d7446f07c`.
Source is based on `b879718` plus the local existing-project deployment changes.

## Reset and preservation

The old `milos-runner` job, its service account and resource permissions were
removed. All 37 legacy Firestore documents were deleted: 2 policies, 34 session
and child documents, and 1 settings document. The fresh agent definition is
published through `service.py`; there are no migrated sessions.

Before deletion, a successful Firestore export saved all 37 documents to:
`gs://milos-20260827-milos-evidence/reset-20260912/firestore`.
The prior Terraform state is also saved outside the repository at
`../reset-20260912/legacy.tfstate`. The reset audit directory is private to the
local user and is not part of image builds.

The project, billing attachment, Terraform state bucket, old storage/evidence
buckets, audit logs and `anthropic-api-key` secret were retained. Secret version
`1`, created `2026-08-30T06:21:47Z`, remains enabled. No secret value was changed.
The old runner's permission to read it was removed with that retired identity.

## Verified

- 68 tests pass; Ruff, formatting and strict mypy pass.
- Terraform validates and formats successfully. The final full plan reports **no changes** (exit code 0).
- Cleanup apply completed: 11 legacy resources removed and the audit sink updated.
- All five Cloud Run services report Ready, with 100% traffic on their new revisions.
- A temporary Cloud Run job under the runner's identity and VPC reached `/health`
  on the internal API (200) and initialized the web connector at `/mcp` (200).
- The same job could not reach `https://example.com` (`Network is unreachable`).
- The verification execution `milos-deployment-check-j2pvc` succeeded. Its temporary
  job was removed afterward; the execution logs remain in Cloud Logging.

## Activation still required

This is a deployed development stack, **not a verified working agent service**.

1. Configure IAP OAuth for the personal project in the
   [Cloud Run security page](https://console.cloud.google.com/run/detail/asia-northeast1/milos-api-public/security?project=milos-20260827).
   The public API currently returns `502 Empty Google Account OAuth client ID(s)/secret(s)`.
   Adding gcloud's built-in OAuth client for programmatic access was rejected by
   Google because the client is not in the same organization as this resource.
   IAP and its IAM restriction remain enabled; authentication was not bypassed.
2. Resolve the model backend. The current runner uses Vertex AI. The repository's
   example `claude-sonnet-5@20260601` and the catalog-listed
   `claude-sonnet-4-5@20250929` both returned 404 from this project's Vertex endpoint.
   The latter is staged in `analyst.yaml`; it needs access before enabling the agent.
   [Vertex model card](https://console.cloud.google.com/vertex-ai/publishers/anthropic/model-garden/claude-sonnet-4-5?project=milos-20260827).
   Using the retained Anthropic API key instead requires an explicit runner/backend
   and network change; it is not silently substituted for Vertex credentials.
3. After authentication and a real model request succeed, enable `analyst`, perform
   the hook/audit/session smoke tests in `docs/operations.md`, and verify a complete
   agent run. Self-approval remains forbidden; Bash/web fetch require another
   authorized approver, so the sole user cannot approve their own session.

The single-project variant has no organization controls or VPC Service Controls
perimeter and does not claim the four-project design's administrative separation.
