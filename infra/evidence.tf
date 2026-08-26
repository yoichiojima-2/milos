# --- the evidence bucket: where "immutable audit record" is actually true ---
# Firestore has no immutability primitive — the journal's doc-id==uuid rule
# stops overwrites, but a project owner could still delete documents. The
# evidence bucket is the record: versioned, CMEK-encrypted, and under a
# retention policy, so an exported bundle cannot be modified or deleted until
# retention expires. With lock_evidence_retention = true that holds for
# everyone, project owners included, irreversibly.
#
# Firestore is the working copy; this bucket is the record. Documented
# operating procedure (docs/compliance/controls.md): export monthly.

resource "google_storage_bucket" "evidence" {
  name                        = "${var.project}-milos-evidence"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  encryption {
    default_kms_key_name = google_kms_crypto_key.evidence.id
  }

  versioning {
    enabled = true
  }

  retention_policy {
    retention_period = var.evidence_retention_days * 86400
    is_locked        = var.lock_evidence_retention
  }

  depends_on = [
    google_project_service.apis,
    google_kms_crypto_key_iam_member.gcs_evidence,
  ]
}
