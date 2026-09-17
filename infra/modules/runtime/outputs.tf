output "public_url" {
  value = google_cloud_run_v2_service.public.uri
}

output "internal_url" {
  value = local.internal_url
}

output "connector_urls" {
  value = local.connector_urls
}

output "runner_service_accounts" {
  value = { for id, sa in google_service_account.runner : id => sa.email }
}

output "connector_service_account" {
  value = google_service_account.connector.email
}

output "snapshot_bucket" {
  value = google_storage_bucket.snapshots.name
}

output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project}/${google_artifact_registry_repository.images.repository_id}"
}

output "build_service_account" {
  description = "Pass to `gcloud builds submit --service-account`."
  value       = google_service_account.build.email
}

output "build_source_bucket" {
  description = "Pass as `gcloud builds submit --gcs-source-staging-dir=gs://<bucket>/source`."
  value       = google_storage_bucket.build_source.name
}

output "workspace_service_accounts" {
  description = "Agent id -> the identity its BigQuery jobs run as."
  value       = { for id, sa in google_service_account.workspace : id => sa.email }
}
