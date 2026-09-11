# Infrastructure

Terraform for the platform, one module per concern and one root per environment.

```
modules/
  foundation   the four projects (runtime, logging, egress, data) under the folder; APIs
  network      runtime VPC: no NAT, restricted VIP, default-deny egress
  runtime      API (public + internal), runner jobs per agent, internal connector,
               Firestore, snapshot bucket, registries, token key, scheduler, alerts
  egress       egress VPC with NAT, connector + web fetch services, FQDN rules, secrets
  logging      locked audit bucket, folder sink, lien
  data         a data project for one sensitivity class
  perimeter    VPC Service Controls (dry-run by default)
envs/
  dev          wires the modules; copy for stg and prod
```

`terraform fmt -recursive -check infra` and `terraform -chdir=infra/envs/dev validate` run in CI. Apply happens from `main` with Workload Identity Federation; see `docs/operations.md`.

Organization-level items are inputs, not resources: the folder, the billing account, the Access Context Manager policy, and the folder organization policies (`run.allowedVPCEgress=all-traffic`, `restrictServiceUsage`).

For a development deployment inside one existing project without an organization,
use [envs/existing-project](envs/existing-project/README.md). It retains separate
runtime/egress networks, but does not provide the four-project administrative
separation or organization controls. The Milos deployment status and remaining
activation steps are recorded in [deployments/existing-project](../deployments/existing-project/README.md).
