# Sample prompts for the analyst

Each starts a session through the public API; the operator and approver are
the two CLI identities (`infra/envs/dev/operators.tf`).

```sh
export MILOS_API_URL=$(terraform -chdir=infra/envs/dev output -raw public_url)
export MILOS_ID_TOKEN=$(scripts/iap-token.sh milos-operator@<runtime>.iam.gserviceaccount.com "$MILOS_API_URL")
milos run analyst "<prompt>" --approver milos-approver@<runtime>.iam.gserviceaccount.com --follow
```

**Needs no approval** (list_files, read_file, Write are allowed by the definition):

- Summarise last week's numbers. Compare each region with the week before and
  call out anything that moved more than 10%.
- Read every weekly file and write a four-week trend of revenue and refunds per
  region as a markdown table, then one paragraph of interpretation.
- Which region has the highest support tickets per order over the last four
  weeks? Show the numbers behind the answer.

**Parks the session for a human** (Bash and web_fetch need approval; approve as
the other identity with `milos allow <session> <tool_use_id>`):

- Compute total revenue across all weekly files with a shell one-liner, then
  write it to report.md.
- Fetch https://www.bankofjapan.or.jp/en/ and note today's headline next to the
  weekly summary.

**Should be refused by the gate** (tools outside the definition):

- Edit the weekly files to remove refunds. (`Edit` is not allowed.)
