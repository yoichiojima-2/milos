output "bucket" {
  value = google_storage_bucket.data.name
}

output "datasets" {
  value = keys(google_bigquery_dataset.this)
}
