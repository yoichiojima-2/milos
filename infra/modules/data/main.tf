# A data project for one sensitivity class. Nothing here runs code: it holds
# classified data and grants read access to the internal connector's identity
# and nobody else. Retention is enforced by rule, per class.

resource "google_storage_bucket" "data" {
  project                     = var.project
  name                        = "${var.project}-data"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  labels = {
    classification = lower(var.classification)
    managed_by     = "terraform"
  }

  versioning {
    enabled = true
  }

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = var.retention_days
    }
  }
}

resource "google_storage_bucket_iam_member" "readers" {
  for_each = toset(var.reader_service_accounts)

  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${each.value}"
}

resource "google_bigquery_dataset" "this" {
  for_each = var.datasets

  project                     = var.project
  dataset_id                  = each.key
  location                    = var.region
  description                 = each.value.description
  default_table_expiration_ms = var.retention_days * 24 * 60 * 60 * 1000

  labels = {
    classification = lower(var.classification)
    source         = each.value.source
  }
}

resource "google_bigquery_dataset_iam_member" "readers" {
  for_each = {
    for pair in setproduct(keys(var.datasets), var.reader_service_accounts) :
    "${pair[0]}/${pair[1]}" => { dataset = pair[0], sa = pair[1] }
  }

  project    = var.project
  dataset_id = google_bigquery_dataset.this[each.value.dataset].dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${each.value.sa}"
}
