# Sample scripts

Small programs that drive milos through its Python client (`milos.client.Client`)
rather than the `milos` command. They run from the repository root with the
environment set once:

```sh
export MILOS_API_URL=$(terraform -chdir=infra/envs/dev output -raw public_url)
export MILOS_RUNTIME_PROJECT=$(terraform -chdir=infra/envs/dev output -json projects | jq -r .runtime)
```

| Script | Acts as | Shows |
| --- | --- | --- |
| `run_and_follow.py` | operator | Start a session and stream its journal; exit code says whether it is waiting for a person. |
| `approve_pending.py` | approver | Decide parked tool calls by a visible policy, once or continuously. The API refuses an approver who is also the operator. |
| `weekly_report.py` | operator | An unattended run: idempotent per week through `client_request_id`, needs no approval, prints the report. |

`_identity.py` mints the IAP tokens for the two service accounts; every script
imports it. The Anthropic key, Vertex region and everything else about the model
live in the deployment, not here: a script only ever names an agent and a prompt.

Sample data for these prompts is in `samples/data`; prompts to try by hand are
in `samples/prompts.md`.
