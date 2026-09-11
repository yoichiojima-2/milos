# Existing-project deployment

One project, no organization. This root wires the `network`, `egress`, `runtime`
and `data` modules into an existing project and keeps audit logs and operator
records in the same project (`audit.tf`). It does not create projects, attach
billing, or manage folder policies or a VPC Service Controls perimeter, so it has
none of the administrative separation `envs/dev` provides: the project owner can
administer every component, including the audit records. Moving the project
into a folder later is a separate migration; do not apply the project-creating
foundation over this state.

```sh
cp terraform.tfvars.example terraform.tfvars
terraform init -backend-config=bucket=<state bucket>
terraform plan -out=plan.tfplan
terraform apply plan.tfplan
```

**Users.** `users` lists the Google accounts allowed through IAP. Each must also
appear in the agent definition's `allowed_users`; `allowed_groups` may be empty.
Identity verification and the rule against approving your own session still
apply, so a single user cannot run a tool that needs approval. Do not set
`MILOS_DEV_USER` on Cloud Run.

**IAP without an organization** needs a one-time custom OAuth configuration in
the Console, per [Google's custom OAuth setup](https://docs.cloud.google.com/iap/docs/custom-oauth-configuration).
Until it is done the public service answers `502 Empty Google Account OAuth
client ID(s)/secret(s)`. Do not disable IAP to get past it.

**Connectors.** This deployment has no SaaS secrets, so the `egress` connector
points at the credential-free web-fetch service.

**Model access.** Confirm the definition's model answers through Vertex AI in
this project before enabling an agent.
