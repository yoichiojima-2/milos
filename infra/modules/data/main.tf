# A data project for one sensitivity class. Nothing here runs code: it holds
# classified data and grants read access to the internal connector's identity,
# plus one workspace dataset per agent that its workspace identity alone may
# write. Retention is enforced by rule, per class.

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

# Keyed by name: the emails are unknown at plan time on a fresh project, and a
# positional key would move every binding after one that is removed.
resource "google_storage_bucket_iam_member" "readers" {
  for_each = var.reader_service_accounts

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
    for pair in setproduct(keys(var.datasets), keys(var.reader_service_accounts)) :
    "${pair[0]}/${pair[1]}" => { dataset = pair[0], sa = var.reader_service_accounts[pair[1]] }
  }

  project    = var.project
  dataset_id = google_bigquery_dataset.this[each.value.dataset].dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${each.value.sa}"
}

# --- agent workspaces --------------------------------------------------------------
#
# One dataset per agent, `agent_<id>`, written only by that agent's workspace
# identity (the internal connector impersonates it per call). The same identity
# reads the shared datasets the agent's definition names, and runs its query
# jobs in this project so the bytes are billed and logged next to the data.

resource "google_bigquery_dataset" "workspace" {
  for_each = var.workspaces

  project                     = var.project
  dataset_id                  = "agent_${replace(each.key, "-", "_")}"
  location                    = var.region
  description                 = "Workspace of agent ${each.key}: tables it derives for its tasks"
  default_table_expiration_ms = var.retention_days * 24 * 60 * 60 * 1000

  labels = {
    classification = lower(var.classification)
    source         = "agent"
    agent          = each.key
  }
}

resource "google_bigquery_dataset_iam_member" "workspace_editor" {
  for_each = var.workspaces

  project    = var.project
  dataset_id = google_bigquery_dataset.workspace[each.key].dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${each.value.service_account}"
}

# A dataset an agent may read must be one this module manages; a name that is
# not in var.datasets fails the plan, which is the check that the definition
# and the infrastructure agree.
resource "google_bigquery_dataset_iam_member" "workspace_readers" {
  for_each = {
    for pair in flatten([
      for id, w in var.workspaces : [for ds in w.datasets : { key = "${id}/${ds}", agent = id, dataset = ds }]
    ]) : pair.key => pair
  }

  project    = var.project
  dataset_id = google_bigquery_dataset.this[each.value.dataset].dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${var.workspaces[each.value.agent].service_account}"
}

resource "google_project_iam_member" "workspace_jobs" {
  for_each = var.workspaces

  project = var.project
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${each.value.service_account}"
}
