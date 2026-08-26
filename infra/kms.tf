# --- CMEK: customer-managed keys for everything that stores milos data ---
# (ISO 27001 A.8.24.) Two keys on one keyring: `data` encrypts the working
# copies (Firestore, the state bucket), `evidence` encrypts the evidence
# bucket — separate so the evidence key's lifecycle can outlive a rotation or
# revocation decision made about the working data.
#
# A destroyed keyring/key is unusable but its name is retired forever, and
# destroying a key with data encrypted under it makes that data unreadable —
# Terraform's prevent_destroy makes that a deliberate two-step, not a side
# effect of a teardown.

resource "google_kms_key_ring" "milos" {
  name       = "milos"
  location   = var.region
  depends_on = [google_project_service.apis]

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_kms_crypto_key" "data" {
  name            = "milos-data"
  key_ring        = google_kms_key_ring.milos.id
  rotation_period = var.kms_rotation_period

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_kms_crypto_key" "evidence" {
  name            = "milos-evidence"
  key_ring        = google_kms_key_ring.milos.id
  rotation_period = var.kms_rotation_period

  lifecycle {
    prevent_destroy = true
  }
}

# The service agents that encrypt on milos's behalf need to use the keys.
# Firestore's agent only exists once forced into being (the identity
# resource); GCS's is exposed by a data source.

data "google_project" "project" {}

resource "google_project_service_identity" "firestore" {
  provider   = google-beta
  service    = "firestore.googleapis.com"
  depends_on = [google_project_service.apis]
}

data "google_storage_project_service_account" "gcs" {
  depends_on = [google_project_service.apis]
}

resource "google_kms_crypto_key_iam_member" "firestore_data" {
  crypto_key_id = google_kms_crypto_key.data.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.firestore.email}"
}

resource "google_kms_crypto_key_iam_member" "gcs_data" {
  crypto_key_id = google_kms_crypto_key.data.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}"
}

resource "google_kms_crypto_key_iam_member" "gcs_evidence" {
  crypto_key_id = google_kms_crypto_key.evidence.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}"
}
