# Operations

## Deploy

Prerequisites: a folder you can create projects in, a billing account, `terraform` ≥ 1.9, `gcloud`, and a versioned GCS bucket for state.

```sh
cd infra/envs/dev
cp terraform.tfvars.example terraform.tfvars   # fill in folder, billing, groups
terraform init -backend-config="bucket=<state bucket>"
terraform apply
```

The first apply creates the projects and the registry before any image exists, so the Cloud Run resources fail until one is pushed. Build the image, then apply again with its tag:

```sh
REPO=$(terraform output -raw image_repository)
gcloud builds submit --project $(terraform output -json projects | jq -r .runtime) --tag $REPO/milos:$(git rev-parse --short HEAD) ..
terraform apply -var image=$REPO/milos:$(git rev-parse --short HEAD)
```

Then publish the definitions. Each definition's `runner_sa` must equal the identity Terraform created for it (`terraform output runner_service_accounts`):

```sh
export MILOS_PROJECT=$(terraform output -json projects | jq -r .runtime)
uv run milos agents validate ../../../agents/*.yaml
uv run milos agents publish ../../../agents/*.yaml
uv run milos agents registry        # the generated register
```

Users need `MILOS_API_URL` (`terraform output public_url`) and an identity token for IAP.

## First deploy

The design marks these as things to confirm on real hardware. Do them in order with a non-sensitive agent before connecting any classified data.

1. **IAP audience.** Send one request through IAP and read the `aud` claim of `x-goog-iap-jwt-assertion`; set `iap_audience` to it. Until it matches, every public request is rejected with 401.
2. **The hook.** Start a session whose first message needs `Read`. The event stream must show `agent.tool_use` then `tool.permitted` before `tool.result`, and the audit log must contain the matching entry.
3. **Resume after approval.** Trigger a `Bash` call. The session must reach `requires_action`, a snapshot `1` must exist, and after `milos allow` a new execution must resume the SDK session (`resume` in the manifest) and execute the re-issued call. If the SDK does not re-issue the call after the continuation prompt, the fallback is to keep the runner alive while waiting; see soa-notes.
4. **Internal address.** From inside a runner (`Bash: curl -sS $MILOS_API_URL/health`) the internal service must answer over the restricted VIP. If `run.app` does not resolve to the VIP, adjust the private zone in `modules/network`.
5. **No internet.** `curl https://example.com` inside a runner must fail. `curl` from the egress connector must reach only `allowed_fqdns`.
6. **Connector token lifetime.** The runner passes an identity token to connectors in MCP headers at start; tokens expire after an hour. Confirm a run longer than an hour still reaches the connector, or lower `runner_timeout`.
7. **Synchronous audit.** Stop the logging API (deny `logging.logWriter` temporarily) and confirm a tool request is denied and no permission document exists.

## Runbooks

**Approve or deny.** `milos sessions` shows `pending=` with the tool use id. `milos allow <session> <tool_use_id>` or `milos deny …`. The operator cannot decide on their own session; the decision needs someone in the agent's `allowed_groups` (and in the session's `approvers` if set). Expired requests are denied by inspection after `approval_ttl_sec`.

**Stop one session.** `milos interrupt` ends the current turn; `milos terminate` ends the session for good.

**Stop an agent.** `milos agents disable <agent>` (Firestore, CI credentials). Every session of that agent stops at its next tool request or poll; new sessions are refused. `enable` reverses it.

**Stalled run.** Inspection restarts a run silent for 60 s once; if the restart stalls too the session becomes `needs_attention`. Look at the job's execution logs, fix the cause, and send a message to restart it, or terminate it.

**Change a definition.** Edit the YAML, open a pull request (CI validates), merge, publish. Running sessions keep the version they started with; new sessions take the latest.

**Rotate the token key.** Add a new secret version; redeploy both API services. Sessions running across the rotation lose their session token and stop at the next poll; inspection restarts them with fresh tokens.

**Retention.** Snapshots expire by bucket rule (`snapshot_retention_days`). Firestore sessions are kept; deletion is a deliberate operator action outside the API. The audit bucket keeps `retention_days` and, once `locked = true`, cannot be shortened.

## Local development

```sh
uv sync --group dev
uv run pytest -q
```

An API without GCP, against the Firestore emulator:

```sh
gcloud emulators firestore start --host-port=localhost:8080 &
MILOS_DEV_USER=you@example.com MILOS_TOKEN_KEY=dev MILOS_PROJECT=local \
FIRESTORE_EMULATOR_HOST=localhost:8080 uv run milos serve api --port 8080
MILOS_API_URL=http://localhost:8080 MILOS_ID_TOKEN=x uv run milos run analyst "hello"
```

With `MILOS_DEV_USER` set, the API trusts that email, logs audit entries to stderr, and creates sessions without launching jobs. A runner can be started by hand against the internal role of the same API with the session and lease tokens printed by the job launcher.
