# Foundation: the projects, split by data sensitivity rather than by team.
#
#   runtime  API, runner jobs, scheduler, Firestore, snapshot bucket, internal connector
#   logging  the locked log bucket and nothing else
#   egress   the only projects with a NAT: SaaS connector, web fetch, secrets
#   data     classified data (one per sensitivity domain; this module makes one)
#
# All four sit under one folder so folder-level organization policies and the
# aggregated audit-log sink cover them at once.

locals {
  projects = {
    runtime = [
      "run.googleapis.com",
      "firestore.googleapis.com",
      "storage.googleapis.com",
      "artifactregistry.googleapis.com",
      "aiplatform.googleapis.com",
      "cloudscheduler.googleapis.com",
      "secretmanager.googleapis.com",
      "iap.googleapis.com",
      "cloudidentity.googleapis.com",
      "compute.googleapis.com",
      "dns.googleapis.com",
      "logging.googleapis.com",
      "monitoring.googleapis.com",
      "iam.googleapis.com",
      "iamcredentials.googleapis.com", # the internal connector impersonates workspace identities
      "cloudresourcemanager.googleapis.com",
    ]
    logging = [
      "logging.googleapis.com",
      "cloudresourcemanager.googleapis.com",
    ]
    egress = [
      "run.googleapis.com",
      "compute.googleapis.com",
      "dns.googleapis.com",
      "secretmanager.googleapis.com",
      "logging.googleapis.com",
      "iam.googleapis.com",
    ]
    data = [
      "storage.googleapis.com",
      "bigquery.googleapis.com",
      "logging.googleapis.com",
    ]
  }

  services = {
    for pair in flatten([
      for role, apis in local.projects : [
        for api in apis : { key = "${role}/${api}", role = role, api = api }
      ]
    ]) : pair.key => pair
  }
}

resource "google_project" "this" {
  for_each = local.projects

  name            = "${var.prefix}-${each.key}-${var.env}"
  project_id      = "${var.prefix}-${each.key}-${var.env}"
  folder_id       = var.folder_id
  billing_account = var.billing_account
  deletion_policy = var.deletion_policy

  labels = {
    env        = var.env
    role       = each.key
    managed_by = "terraform"
  }
}

resource "google_project_service" "apis" {
  for_each = local.services

  project            = google_project.this[each.value.role].project_id
  service            = each.value.api
  disable_on_destroy = false
}
