# --- access logging: the "who read what" layer app code cannot provide ---
# (ISO 27001 A.8.15.) milos's journal records what agents did; Cloud Audit
# Logs record what *people and service accounts* did to the stores themselves
# — reads of Firestore documents, GCS objects, and KMS key operations. Data
# Access logs are off by default in GCP; this turns them on for exactly the
# services holding milos data and routes them to a bucket with its own
# retention.

resource "google_project_iam_audit_config" "firestore" {
  project = var.project
  service = "firestore.googleapis.com"

  audit_log_config {
    log_type = "ADMIN_READ"
  }
  audit_log_config {
    log_type = "DATA_READ"
  }
  audit_log_config {
    log_type = "DATA_WRITE"
  }
}

resource "google_project_iam_audit_config" "storage" {
  project = var.project
  service = "storage.googleapis.com"

  audit_log_config {
    log_type = "ADMIN_READ"
  }
  audit_log_config {
    log_type = "DATA_READ"
  }
  audit_log_config {
    log_type = "DATA_WRITE"
  }
}

resource "google_project_iam_audit_config" "kms" {
  project = var.project
  service = "cloudkms.googleapis.com"

  audit_log_config {
    log_type = "ADMIN_READ"
  }
  audit_log_config {
    log_type = "DATA_READ"
  }
  audit_log_config {
    log_type = "DATA_WRITE"
  }
}

# A dedicated logging bucket with explicit retention, so the access record
# survives the _Default bucket's 30-day window.
resource "google_logging_project_bucket_config" "audit" {
  project        = var.project
  location       = var.region
  bucket_id      = "milos-audit"
  retention_days = var.audit_log_retention_days
  depends_on     = [google_project_service.apis]
}

resource "google_logging_project_sink" "audit" {
  name        = "milos-audit"
  description = "Admin Activity + Data Access logs for the services holding milos data"
  destination = "logging.googleapis.com/${google_logging_project_bucket_config.audit.id}"

  filter = <<-EOT
    logName:("cloudaudit.googleapis.com%2Factivity" OR "cloudaudit.googleapis.com%2Fdata_access")
    AND protoPayload.serviceName=("firestore.googleapis.com" OR "storage.googleapis.com" OR "cloudkms.googleapis.com" OR "run.googleapis.com")
  EOT

  unique_writer_identity = true
}

# The sink writes with its own service identity; grant it into the bucket.
resource "google_project_iam_member" "audit_sink_writer" {
  project = var.project
  role    = "roles/logging.bucketWriter"
  member  = google_logging_project_sink.audit.writer_identity
}
