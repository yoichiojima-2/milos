output "projects" {
  value = module.foundation.project_ids
}

output "public_url" {
  description = "Set MILOS_API_URL to this."
  value       = module.runtime.public_url
}

output "internal_url" {
  value = module.runtime.internal_url
}

output "connector_urls" {
  value = module.runtime.connector_urls
}

output "image_repository" {
  description = "Push the image here; pass the tag back as var.image."
  value       = module.runtime.image_repository
}

output "runner_service_accounts" {
  description = "Paste into each definition's runner_sa."
  value       = module.runtime.runner_service_accounts
}

output "build_service_account" {
  value = module.runtime.build_service_account
}

output "build_source_bucket" {
  value = module.runtime.build_source_bucket
}

output "workspace_service_accounts" {
  description = "Agent id -> the identity its BigQuery jobs run as."
  value       = module.runtime.workspace_service_accounts
}

output "workspace_datasets" {
  description = "Agent id -> its BigQuery workspace dataset."
  value       = module.data.workspace_datasets
}
