output "project_ids" {
  description = "Project ids by role: runtime, logging, egress, data."
  value       = { for role, p in google_project.this : role => p.project_id }
}

output "project_numbers" {
  value = { for role, p in google_project.this : role => p.number }
}

output "services" {
  description = "Depend on this so resources wait for API enablement."
  value       = google_project_service.apis
}
