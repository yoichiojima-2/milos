# Logging: one locked bucket that nobody, project owners included, can shorten
# or delete, and a lien so the project itself cannot be deleted around it.
#
# The folder-level sink collects Cloud Audit Logs from every project under the
# folder plus the platform's own audit entries (`milos-audit`), which the API
# writes synchronously before permitting a tool call.

resource "google_logging_project_bucket_config" "audit" {
  project        = var.project
  location       = var.location
  bucket_id      = "audit"
  description    = "Audit record: Cloud Audit Logs for the folder and milos tool-request entries"
  retention_days = var.retention_days
  # Locking is irreversible: the bucket cannot be deleted and retention cannot
  # be reduced until every entry has aged out. Set once the design is final.
  locked = var.locked
}

resource "google_logging_folder_sink" "audit" {
  folder           = var.folder_id
  name             = "milos-audit"
  include_children = true
  destination      = "logging.googleapis.com/${google_logging_project_bucket_config.audit.id}"
  filter           = <<-EOT
    logName:"cloudaudit.googleapis.com" OR logName:"logs/${var.audit_log_name}"
  EOT
}

resource "google_project_iam_member" "sink_writer" {
  project = var.project
  role    = "roles/logging.bucketWriter"
  member  = google_logging_folder_sink.audit.writer_identity
}

resource "google_resource_manager_lien" "audit" {
  parent       = "projects/${var.project_number}"
  restrictions = ["resourcemanager.projects.delete"]
  origin       = "milos"
  reason       = "Holds the audit log bucket; deleting the project would delete the record"
}
