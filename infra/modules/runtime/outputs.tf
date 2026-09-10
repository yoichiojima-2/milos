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
