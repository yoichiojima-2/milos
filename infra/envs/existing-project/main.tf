# Personal development deployment in an existing project. Project creation,
# organization policies, folder sinks and VPC Service Controls are not managed here.
terraform {
  required_version = ">= 1.9"
  backend "gcs" {
    prefix = "terraform/state"
  }
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.40"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 6.40"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6"
    }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}
provider "google-beta" {
  project = var.project
  region  = var.region
}

data "google_project" "existing" {
  project_id = var.project
}

resource "google_project_service" "apis" {
  for_each = toset([
    "aiplatform.googleapis.com", "artifactregistry.googleapis.com", "compute.googleapis.com",
    "dns.googleapis.com", "firestore.googleapis.com", "iam.googleapis.com", "logging.googleapis.com",
    "run.googleapis.com", "secretmanager.googleapis.com", "storage.googleapis.com",
    "cloudscheduler.googleapis.com", "iap.googleapis.com", "cloudidentity.googleapis.com",
    "monitoring.googleapis.com", "cloudresourcemanager.googleapis.com", "cloudbuild.googleapis.com",
  ])
  project            = var.project
  service            = each.value
  disable_on_destroy = false
}

module "network" {
  source     = "../../modules/network"
  project    = var.project
  region     = var.region
  depends_on = [google_project_service.apis]
}

module "egress" {
  source                  = "../../modules/egress"
  project                 = var.project
  project_number          = data.google_project.existing.number
  region                  = var.region
  image                   = var.image
  api_internal_url        = "https://milos-api-internal-${data.google_project.existing.number}.${var.region}.run.app"
  runner_service_accounts = [for id in var.agent_ids : "milos-runner-${id}@${var.project}.iam.gserviceaccount.com"]
  allowed_fqdns           = []
  secrets                 = {}
  depends_on              = [google_project_service.apis]
}

module "runtime" {
  source             = "../../modules/runtime"
  project            = var.project
  project_number     = data.google_project.existing.number
  region             = var.region
  firestore_location = var.region
  vertex_region      = var.vertex_region
  image              = var.image
  network_id         = module.network.network_id
  subnet_id          = module.network.subnet_id
  agent_ids          = var.agent_ids
  users              = var.users
  iap_audience       = "/projects/${data.google_project.existing.number}/locations/${var.region}/services/milos-api-public"
  connector_urls = merge(module.egress.connector_urls, {
    # This deployment has no SaaS secrets; web_fetch uses the credential-free service.
    egress = module.egress.connector_urls.web_fetch
  })
  extra_internal_invokers = module.egress.service_accounts
  depends_on              = [google_project_service.apis]
}

module "data" {
  source                  = "../../modules/data"
  project                 = var.project
  region                  = var.region
  classification          = "C1"
  retention_days          = 365
  reader_service_accounts = [module.runtime.connector_service_account]
  depends_on              = [google_project_service.apis]
}

# Adopt retained resources from the pre-0.2 deployment in the same state.
moved {
  from = google_artifact_registry_repository.milos
  to   = module.runtime.google_artifact_registry_repository.images
}
moved {
  from = google_firestore_database.default
  to   = module.runtime.google_firestore_database.default
}
moved {
  from = google_firestore_backup_schedule.weekly
  to   = module.runtime.google_firestore_backup_schedule.weekly
}

output "public_url" { value = module.runtime.public_url }
output "internal_url" { value = module.runtime.internal_url }
output "runner_service_accounts" { value = module.runtime.runner_service_accounts }
output "image_repository" { value = module.runtime.image_repository }
output "snapshot_bucket" { value = module.runtime.snapshot_bucket }
