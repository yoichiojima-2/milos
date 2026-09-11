# Project-scoped stand-in for modules/logging: audit logs and operator records
# stay in this project, with retention, because there is no logging project.
resource "google_logging_project_bucket_config" "audit" {
  project        = var.project
  location       = var.region
  bucket_id      = "milos-audit"
  retention_days = 365
  lifecycle { prevent_destroy = true }
}

resource "google_logging_project_sink" "audit" {
  project     = var.project
  name        = "milos-audit"
  destination = "logging.googleapis.com/${google_logging_project_bucket_config.audit.id}"
  filter      = "logName:\"cloudaudit.googleapis.com\" OR logName:\"logs/milos-audit\""
}

resource "google_project_iam_audit_config" "firestore" {
  project = var.project
  service = "datastore.googleapis.com"
  audit_log_config { log_type = "ADMIN_READ" }
  audit_log_config { log_type = "DATA_READ" }
  audit_log_config { log_type = "DATA_WRITE" }
}

resource "google_project_iam_audit_config" "storage" {
  project = var.project
  service = "storage.googleapis.com"
  audit_log_config { log_type = "ADMIN_READ" }
  audit_log_config { log_type = "DATA_READ" }
  audit_log_config { log_type = "DATA_WRITE" }
}

# Operator records: database exports and other evidence. Objects are kept a year.
resource "google_storage_bucket" "evidence" {
  project                     = var.project
  name                        = "${var.project}-milos-evidence"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning { enabled = true }
  retention_policy { retention_period = 31536000 }
  lifecycle { prevent_destroy = true }
}
