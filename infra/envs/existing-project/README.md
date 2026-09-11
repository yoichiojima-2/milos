# Existing-project development deployment

This root reuses one project. It does not create projects, attach billing, manage
an organization, or claim the four-project isolation described by `envs/dev`.
Runtime and egress still have separate VPCs and service accounts. The project
owner can administer both, including the audit records.

For the intended organization deployment, use a Milos folder containing separate
runtime, logging, egress and data projects as described in `../dev`. Moving this
existing project into a folder alone does not establish those project boundaries.
Adopting the existing project into that topology requires a separate Terraform
migration; do not apply the project-creating foundation over this state.

Copy `terraform.tfvars.example` to `terraform.tfvars`, then initialize with the
existing state bucket. This root adopts the pre-0.2 state at `terraform/state`.
The `moved` blocks retain Firestore, its backup schedule, and the image registry.
The old runner and its permissions are removed. Historical storage, audit logs,
and `anthropic-api-key` are explicitly retained with deletion protection.
The Anthropic secret is retained only; current runners still use Vertex AI.

```sh
terraform init -backend-config=bucket=milos-20260827-tfstate
terraform plan -out=redeploy.tfplan
terraform apply redeploy.tfplan
```

Review the plan before applying. Resetting Firestore documents is a separate,
destructive operator action: stop old executions, export the database, verify
the export, then delete the old documents before publishing new definitions.
A Terraform apply alone does not erase session data.

Set `users` to the individual Google accounts allowed through IAP and include
those accounts in the agent definition's `allowed_users`. `allowed_groups` may
be empty when explicit users are present. Identity verification and the rule
against approving your own session still apply. Do not set `MILOS_DEV_USER` on
Cloud Run.

Google may require a one-time Console OAuth setup for IAP on projects without
an organization. Also verify that the configured Claude model can answer through
Vertex AI before publishing an enabled agent. The example definition contains
organization-specific placeholders; do not publish it unchanged.

The deployed API health endpoint is `/health`: Cloud Run reserves some paths
ending in `z`, including `/healthz`. Connector addresses in Terraform are service
origins for Google identity-token audiences; the runner appends `/mcp` when
configuring the MCP transport. The credential-free web-fetch service is used for
the `egress` connector in this deployment, since no SaaS secrets are configured.

For IAP on a project without an organization, an empty OAuth configuration can
produce `502 Empty Google Account OAuth client ID(s)/secret(s)` despite a ready
Cloud Run service. Complete the custom OAuth setup in the Cloud Run Console.
The built-in gcloud OAuth client was rejected for programmatic access because it
is not in the same organization as this project. Do not disable IAP to work around
that error. See [Google's custom OAuth setup](https://docs.cloud.google.com/iap/docs/custom-oauth-configuration).
