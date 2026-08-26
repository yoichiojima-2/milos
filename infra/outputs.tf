output "bucket" {
  value = google_storage_bucket.state.name
}

output "evidence_bucket" {
  value = google_storage_bucket.evidence.name
}

output "job" {
  value = google_cloud_run_v2_job.runner.name
}

output "runner_service_account" {
  value = google_service_account.runner.email
}

output "kms_key_ring" {
  value = google_kms_key_ring.milos.id
}

output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project}/${google_artifact_registry_repository.milos.repository_id}"
}
