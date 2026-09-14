# milos

Secure agent platform on Google Cloud. One Python package (`src/milos/`): API, runner, connectors, CLI. Terraform in `infra/`. Design in `docs/design.md`; requirement mapping in `docs/compliance/`.

## Commands

- `uv run pytest -q` — no GCP needed; `tests/fakes.py` stands in for Firestore, Cloud Logging, Cloud Run Jobs, GCS and group lookups
- `uv run ruff check . && uv run ruff format .` — CI checks both
- `uv run mypy` — strict, `src/milos` only; the `Protocol` classes are only enforced here, so keep it green
- `uv run milos agents validate agents/*.yaml deployments/*/*.yaml` — CI runs it; an invalid definition is never published
- `terraform fmt -recursive -check infra && terraform -chdir=infra/envs/dev validate && terraform -chdir=infra/envs/existing-project validate` — CI validates both roots

## Rules that shape the code

- **Every state change goes through `service.py`.** The API, the runner and the connectors call it; nothing else writes to Firestore. A change and its event commit in one transaction (`Service._append`). If you need a new state transition, add it there with its event and a test in `tests/test_service.py`.
- **Order in `permit`:** journal the request → write the audit entry synchronously → create the permission. Never reorder; the audit entry failing must leave no permission (`test_audit_failure_means_no_permission`).
- **Runner writes carry the lease token.** Any new internal route takes `X-Milos-Session` (session token, verified by `auth.SessionTokens`) and `X-Milos-Lease` (checked by `Service._check_lease`).
- **Create-only collections** (`permissions`, `approvals`) are enforced by `Transaction.create`; do not add update methods for them.
- **Models are strict** (`extra="forbid"`). New fields go on the model and, when Firestore needs an index, in `modules/runtime/main.tf`.
- **Definitions are the control unit.** Anything an agent may do must be expressible in `agents/*.yaml` and checked by the validators on `models.AgentVersion`. `allowed_tools` are platform capabilities; the SDK gets its default tools plus the hook, never the list.
- **Fakes mirror the real adapters.** A new adapter (protocol in `src/milos/`) gets a fake in `tests/fakes.py` with the same signature.
- **English everywhere** in this repository: code, comments, docs, commit messages.
- Keep `docs/compliance/requirements.md` and `soa-notes.md` in step with control changes.
