# Sample data

Four weeks of made-up sales numbers, one CSV per ISO week, as the analyst
agent expects to find them in the shared data project:

```
weekly/2026-W34.csv … weekly/2026-W37.csv
week,region,orders,revenue_jpy,refunds_jpy,new_customers,support_tickets
```

Upload to a deployment's data bucket (the internal connector reads it with
`list_files` / `read_file`):

```sh
gcloud storage cp -r samples/data/weekly gs://<data project>-data/
```
