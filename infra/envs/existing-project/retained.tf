# Retain recovery material and the existing credential during a runtime reset.
# These addresses already exist in the legacy remote state.
resource "google_storage_bucket" "state" {
  project                     = var.project
  name                        = "${var.project}-milos"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning { enabled = true }
  lifecycle { prevent_destroy = true }
  lifecycle_rule {
    action { type = "Delete" }
    condition {
      age            = 30
      matches_prefix = ["sessions/"]
    }
  }
  lifecycle_rule {
    action { type = "Delete" }
    condition {
      days_since_noncurrent_time = 30
      with_state                 = "ARCHIVED"
    }
  }
}
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
resource "google_secret_manager_secret" "anthropic_api_key" {
  project   = var.project
  secret_id = "anthropic-api-key"
  replication {
    auto {}
  }
  lifecycle { prevent_destroy = true }
}
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
