output "bucket" {
  value = google_storage_bucket.data.name
}

output "datasets" {
  value = keys(google_bigquery_dataset.this)
}

output "workspace_datasets" {
  description = "Agent id -> its workspace dataset."
  value       = { for id, ds in google_bigquery_dataset.workspace : id => ds.dataset_id }
}
