# The dev environment. stg and prod are copies of this directory with their
# own tfvars and state prefix; the modules are shared.

terraform {
  required_version = ">= 1.9"

  # State lives in a versioned GCS bucket created once by hand (it must
  # outlive every apply):
  #   gcloud storage buckets create gs://<bucket> --location <region> --versioning
  #   terraform init -backend-config="bucket=<bucket>"
  backend "gcs" {
    prefix = "milos/dev"
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
  region = var.region
}

provider "google-beta" {
  region = var.region
}

locals {
  env = "dev"
  # Identities are named deterministically so the two Cloud Run projects can
  # reference each other without a dependency cycle.
  runner_service_accounts = [
    for id in var.agent_ids :
    "milos-runner-${id}@${module.foundation.project_ids.runtime}.iam.gserviceaccount.com"
  ]
}

module "foundation" {
  source = "../../modules/foundation"

  folder_id       = var.folder_id
  billing_account = var.billing_account
  prefix          = var.prefix
  env             = local.env
  deletion_policy = "DELETE"
}

module "network" {
  source = "../../modules/network"

  project = module.foundation.project_ids.runtime
  region  = var.region

  depends_on = [module.foundation]
}

module "logging" {
  source = "../../modules/logging"

  project        = module.foundation.project_ids.logging
  project_number = module.foundation.project_numbers.logging
  location       = var.region
  folder_id      = var.folder_id
  retention_days = 400
  locked         = false # dev; prod locks

  depends_on = [module.foundation]
}

module "egress" {
  source = "../../modules/egress"

  project                 = module.foundation.project_ids.egress
  project_number          = module.foundation.project_numbers.egress
  region                  = var.region
  image                   = var.image
  api_internal_url        = "https://milos-api-internal-${module.foundation.project_numbers.runtime}.${var.region}.run.app"
  runner_service_accounts = local.runner_service_accounts
  allowed_fqdns           = var.allowed_fqdns
  secrets                 = var.egress_secrets

  depends_on = [module.foundation]
}

module "runtime" {
  source = "../../modules/runtime"

  project                      = module.foundation.project_ids.runtime
  project_number               = module.foundation.project_numbers.runtime
  region                       = var.region
  firestore_location           = var.region
  vertex_region                = var.vertex_region
  image                        = var.image
  network_id                   = module.network.network_id
  subnet_id                    = module.network.subnet_id
  agent_ids                    = var.agent_ids
  users_group                  = var.users_group
  operator_service_accounts    = [for sa in google_service_account.operators : sa.email]
  iap_audience                 = var.iap_audience
  connector_urls               = module.egress.connector_urls
  extra_internal_invokers      = module.egress.service_accounts
  image_puller_project_numbers = [module.foundation.project_numbers.egress]
  schedules                    = var.schedules
  alert_email                  = var.alert_email

  depends_on = [module.foundation]
}

module "data" {
  source = "../../modules/data"

  project                 = module.foundation.project_ids.data
  region                  = var.region
  classification          = "C1"
  retention_days          = 365
  reader_service_accounts = { connector = module.runtime.connector_service_account }

  depends_on = [module.foundation]
}

module "perimeter" {
  count  = var.access_policy_id == null ? 0 : 1
  source = "../../modules/perimeter"

  access_policy_id = var.access_policy_id
  name             = "milos_${local.env}"
  project_numbers  = values(module.foundation.project_numbers)
  admin_members    = var.perimeter_admins
  dry_run          = true
}
