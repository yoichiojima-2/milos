# milos

Secure agent platform on Google Cloud. One Python package (`src/milos/`): API, runner, connectors, CLI. Terraform in `infra/`. Design in `docs/design.md`; requirement mapping in `docs/compliance/`.

## Commands

- `uv run pytest -q` — no GCP needed; `tests/fakes.py` stands in for Firestore, Cloud Logging, Cloud Run Jobs, GCS and group lookups
- `uv run ruff check . && uv run ruff format .` — CI checks both
- `uv run mypy` — strict, `src/milos` only; the `Protocol` classes are only enforced here, so keep it green
- `uv run milos agents validate agents/*.yaml deployments/*/*.yaml` — CI runs it; an invalid definition is never published
- `terraform fmt -recursive -check infra && terraform -chdir=infra/envs/dev validate` — CI validates it

## Rules that shape the code

- **Every state change goes through `service.py`.** The API calls it; nothing else writes to Firestore, the CLI included. A state change is one method running one function under `Service._commit`; events are appended only with `Tx.append`, sessions written only through `Tx.save`, and the function returns a `Change` naming the session whose job to launch. A new transition goes there with its event and a test in `tests/test_service.py`.
- **Order in `permit`:** first transaction journals the request (and parks the session when a person must decide) → the audit entry is written synchronously → a second transaction creates the permission. Never reorder; the audit entry failing must leave no permission (`test_audit_failure_means_no_permission`).
- **Authorization lives in `access.py`.** `api.py` only applies `can_view`, `can_operate`, `can_decide` and `Access.member`/`Access.admin`; publishing and enabling need `MILOS_ADMIN_GROUP`; the scheduler routes accept only `MILOS_SCHEDULER_SA`.
- **Wire types live in `models.py`.** Everything the API accepts or returns is a model there; `runner.py`, `control.py`, `client.py` and `connector.py` never import `service.py`. Every client of the API is a thin layer over `http.Api`.
- **Runner writes carry the lease token.** Any new internal route takes `X-Milos-Session` (session token, verified by `auth.SessionTokens`) and `X-Milos-Lease` (checked by `Service._check_lease`).
- **Create-only collections** (`permissions`, `approvals`, `requests`) are enforced by `Transaction.create`; do not add update methods for them.
- **Models are strict** (`extra="forbid"`). New fields go on the model and, when Firestore needs an index, in `modules/runtime/main.tf`.
- **Definitions are the control unit.** Anything an agent may do must be expressible in `agents/*.yaml` and checked by the validators on `models.AgentVersion`. `allowed_tools` are platform capabilities; the SDK gets its default tools plus the hook, never the list.
- **Fakes mirror the real adapters.** A new adapter (protocol in `src/milos/`) gets a fake in `tests/fakes.py` with the same signature. Store semantics the fake only mirrors are proven in `tests/test_store_emulator.py` (`-m emulator`).
- **English everywhere** in this repository: code, comments, docs, commit messages.
- Keep `docs/compliance/requirements.md` and `soa-notes.md` in step with control changes.
