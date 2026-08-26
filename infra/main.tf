terraform {
  # State lives in GCS, not on whoever ran apply last. The bucket is versioned
  # and deliberately unmanaged by this config — it has to outlive any single
  # apply, and a state bucket that stores its own state is a bootstrap knot.
  # Point it at your own bucket:
  #   gcloud storage buckets create gs://YOUR_PROJECT-tfstate --versioning
  #   terraform init -backend-config="bucket=YOUR_PROJECT-tfstate"
  backend "gcs" {
    prefix = "terraform/state"
  }

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.47"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 6.47"
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

resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "firestore.googleapis.com",
    "artifactregistry.googleapis.com",
    "aiplatform.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudkms.googleapis.com",
    "logging.googleapis.com",
    # for egress_control (enabled unconditionally: toggling the for_each set
    # on a flag churns every service resource, and disable_on_destroy=false
    # means an unused enabled API costs nothing)
    "compute.googleapis.com",
    "dns.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# --- state: Firestore (control plane) + GCS (per-session state) ---
# Both CMEK-encrypted (kms.tf) and pinned to one region (data residency).

resource "google_firestore_database" "default" {
  name                              = "(default)"
  location_id                       = var.firestore_location
  type                              = "FIRESTORE_NATIVE"
  delete_protection_state           = "DELETE_PROTECTION_ENABLED"
  point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_ENABLED"

  cmek_config {
    kms_key_name = google_kms_crypto_key.data.id
  }

  depends_on = [
    google_project_service.apis,
    google_kms_crypto_key_iam_member.firestore_data,
  ]
}

# Firestore is the only copy of the control plane (GCS holds per-session blobs,
# evidence bundles are point-in-time exports), so keep a rolling weekly backup.
resource "google_firestore_backup_schedule" "weekly" {
  database  = google_firestore_database.default.name
  retention = "1209600s" # 14 days

  weekly_recurrence {
    day = "SUNDAY"
  }
}

# `milos approvals` with no session queries approvals across all sessions;
# collection-group queries need an explicit collection-group-scoped index.
resource "google_firestore_field" "approvals_status" {
  database   = google_firestore_database.default.name
  collection = "approvals"
  field      = "status"

  index_config {
    # custom index_config replaces the field's default single-field indexes,
    # so keep the collection-scoped index that per-session queries rely on
    indexes {
      order       = "ASCENDING"
      query_scope = "COLLECTION"
    }
    indexes {
      order       = "ASCENDING"
      query_scope = "COLLECTION_GROUP"
    }
  }
}

# journal tailing filters one branch and orders by seq (store.list_events);
# equality + order-by on different fields needs a composite index
resource "google_firestore_index" "events_by_branch" {
  database   = google_firestore_database.default.name
  collection = "events"

  fields {
    field_path = "branch"
    order      = "ASCENDING"
  }
  fields {
    field_path = "seq"
    order      = "ASCENDING"
  }
}

# recover_head reads one branch's newest record (branch == + seq DESC);
# composite indexes are direction-sensitive, so the ASC pair above can't serve it
resource "google_firestore_index" "events_by_branch_desc" {
  database   = google_firestore_database.default.name
  collection = "events"

  fields {
    field_path = "branch"
    order      = "ASCENDING"
  }
  fields {
    field_path = "seq"
    order      = "DESCENDING"
  }
}

# the audit trail reads tool_call records out of the journal in write order
# (store.list_tool_calls): type equality + ts order-by
resource "google_firestore_index" "events_by_type" {
  database   = google_firestore_database.default.name
  collection = "events"

  fields {
    field_path = "type"
    order      = "ASCENDING"
  }
  fields {
    field_path = "ts"
    order      = "ASCENDING"
  }
}

resource "google_storage_bucket" "state" {
  name                        = "${var.project}-milos"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  encryption {
    default_kms_key_name = google_kms_crypto_key.data.id
  }

  versioning {
    enabled = true
  }

  # The GCS half of the retention schedule (`milos sessions purge` is the
  # Firestore half): session state expires by rule, not by someone remembering.
  # The tfvars value is the source of truth; mirror it in the milos policy's
  # retention.session_state_days so the evidence shows the same number.
  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age            = var.session_state_retention_days
      matches_prefix = ["sessions/"]
    }
  }
  # Versioning keeps overwritten/deleted generations; expire them so the
  # retention schedule actually deletes data instead of archiving it.
  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      days_since_noncurrent_time = var.session_state_retention_days
      with_state                 = "ARCHIVED"
    }
  }

  depends_on = [
    google_project_service.apis,
    google_kms_crypto_key_iam_member.gcs_data,
  ]
}

resource "google_artifact_registry_repository" "milos" {
  repository_id = "milos"
  location      = var.region
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]

  # Releases push a new image every tag and nothing else deletes them, so the
  # repository grows without bound. Keep enough recent versions to roll back.
  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }
  cleanup_policies {
    id     = "delete-stale"
    action = "DELETE"
    condition {
      older_than = "2592000s" # 30 days; KEEP above still wins for the newest 5
    }
  }
}

# --- secrets: container only; the value is added out-of-band via gcloud ---

resource "google_secret_manager_secret" "anthropic_api_key" {
  secret_id = "anthropic-api-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# --- the sandbox identity: least privilege; a secret only on the escape hatch ---

resource "google_service_account" "runner" {
  account_id   = "milos-runner"
  display_name = "milos sandbox runner"
}

resource "google_project_iam_member" "runner_vertex" {
  project = var.project
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.runner.email}"
}

resource "google_project_iam_member" "runner_firestore" {
  project = var.project
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.runner.email}"
}

resource "google_storage_bucket_iam_member" "runner_bucket" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runner.email}"
}

# Deliberately absent: any grant on the evidence bucket. The sandbox agent
# runs as this service account with a shell, so evidence integrity rests on
# the runner having no path to the bucket at all — exports run with the
# operator's identity, never the sandbox's.

# The shutdown handoff re-triggers this same job when a prompt arrived during
# checkpointing (with the session id as a container override, hence
# runWithOverrides). Scoped to the one job — the runner can start more of
# itself, nothing else.
resource "google_cloud_run_v2_job_iam_member" "runner_runs_job" {
  name     = google_cloud_run_v2_job.runner.name
  location = var.region
  role     = "roles/run.jobsExecutorWithOverrides"
  member   = "serviceAccount:${google_service_account.runner.email}"
}

# Granted only when the sandbox actually calls Anthropic directly, so the
# default deployment keeps a runner identity that can read no secret at all.
resource "google_secret_manager_secret_iam_member" "runner_anthropic_key" {
  count     = var.model_backend == "anthropic" ? 1 : 0
  secret_id = google_secret_manager_secret.anthropic_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runner.email}"
}

# --- the sandbox ---

resource "google_cloud_run_v2_job" "runner" {
  name                = var.job_name
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.runner.email
      timeout         = var.job_timeout
      max_retries     = 0

      containers {
        image = var.image
        resources {
          limits = {
            cpu    = var.cpu
            memory = var.memory
          }
        }
        env {
          name  = "MILOS_PROJECT"
          value = var.project
        }
        env {
          name  = "MILOS_BUCKET"
          value = google_storage_bucket.state.name
        }
        # Where the runner finds itself: the shutdown handoff triggers the
        # next execution of this same job from inside the sandbox.
        env {
          name  = "MILOS_REGION"
          value = var.region
        }
        env {
          name  = "MILOS_JOB"
          value = var.job_name
        }
        env {
          name  = "CLOUD_ML_REGION"
          value = var.vertex_region
        }
        env {
          name  = "MILOS_MODEL_BACKEND"
          value = var.model_backend
        }

        dynamic "env" {
          for_each = var.model_backend == "anthropic" ? [1] : []
          content {
            name = "ANTHROPIC_API_KEY"
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.anthropic_api_key.secret_id
                version = "latest"
              }
            }
          }
        }
      }

      # ALL_TRAFFIC forces every packet — Google APIs included — through the
      # default-deny VPC in network.tf; see the egress rules there.
      dynamic "vpc_access" {
        for_each = var.egress_control ? [1] : []
        content {
          egress = "ALL_TRAFFIC"
          network_interfaces {
            network    = google_compute_network.egress[0].id
            subnetwork = google_compute_subnetwork.runner[0].id
          }
        }
      }
    }
  }

  # The secretAccessor binding must exist before Cloud Run validates the mount,
  # or the job update fails with a permission error on the secret. The NAT and
  # firewall association aren't referenced from the job spec, so order them
  # explicitly — a job attached to a VPC with no NAT or policy yet would run
  # its first sessions with the wrong egress.
  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.runner_anthropic_key,
    google_compute_router_nat.egress,
    google_compute_network_firewall_policy_association.egress,
  ]

  # Releases pin the image by digest via `gcloud run jobs update`; var.image is
  # only the bootstrap tag. Ignore that drift — and the client stamp gcloud
  # leaves — so a post-release apply doesn't roll the pin back to :latest.
  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      client,
      client_version,
    ]
  }
}
