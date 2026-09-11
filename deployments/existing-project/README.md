# Milos on `milos-20260827`

Region `asia-northeast1`, Terraform root `infra/envs/existing-project`, state in
`gs://milos-20260827-tfstate/terraform/state`. `analyst.yaml` is the definition
published here; CI validates it with `agents/*.yaml`.

## Status

- Deployed: `milos-api-public`, `milos-api-internal`, `milos-connector-internal`,
  `milos-egress-connector`, `milos-egress-web-fetch`, the `milos-runner-analyst`
  job and the `milos-inspect` scheduler, on separate runtime and egress networks.
- Verified from a job under the runner's identity: the internal API and the
  connector's MCP endpoint answer over the restricted VIP; the public internet
  does not.
- Public access is restricted to `yoichiojima@gmail.com` through IAP.
- `analyst` v1 is published **disabled**.

## Before the agent can run

1. Configure IAP OAuth for the project ([Cloud Run security page](https://console.cloud.google.com/run/detail/asia-northeast1/milos-api-public/security?project=milos-20260827)).
   The public API returns `502 Empty Google Account OAuth client ID(s)/secret(s)` until then.
2. Get Vertex AI access to the definition's model in this project
   ([model card](https://console.cloud.google.com/vertex-ai/publishers/anthropic/model-garden/claude-sonnet-4-5?project=milos-20260827));
   the current one returns 404.
3. Enable `analyst`, then run the first-deploy checks in `docs/operations.md`.
   Approval-gated tools need a second authorized approver.

This is a single-project development stack without organization controls; see
`docs/compliance/soa-notes.md`.
