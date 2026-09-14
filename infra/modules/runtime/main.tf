# Runtime: the API (deployed twice), one runner job per agent, the internal
# connector, Firestore, the snapshot bucket, the scheduler, and the identities
# that tie them together.

locals {
  public_name   = "${var.name}-api-public"
  internal_name = "${var.name}-api-internal"
  # Cloud Run's deterministic URL; known before the service exists, which lets
  # the internal service be told its own address and jobs be told theirs.
  internal_url  = "https://${local.internal_name}-${var.project_number}.${var.region}.run.app"
  connector_url = "https://${var.name}-connector-internal-${var.project_number}.${var.region}.run.app"

  connector_urls = merge(var.connector_urls, { internal = local.connector_url })

  runner_env = {
    MILOS_INTERNAL_API_URL = local.internal_url
    MILOS_PROJECT          = var.project
    MILOS_SNAPSHOT_BUCKET  = google_storage_bucket.snapshots.name
    MILOS_CONNECTOR_URLS   = jsonencode(local.connector_urls)
    MILOS_VERTEX_REGION    = var.vertex_region
  }

  api_env = {
    MILOS_PROJECT           = var.project
    MILOS_REGION            = var.region
    MILOS_RUNNER_JOB_PREFIX = "${var.name}-runner"
    MILOS_INTERNAL_URL      = local.internal_url
    MILOS_SNAPSHOT_BUCKET   = google_storage_bucket.snapshots.name
    MILOS_CONNECTOR_URLS    = jsonencode(local.connector_urls)
    MILOS_VERTEX_REGION     = var.vertex_region
    MILOS_IAP_AUDIENCE      = var.iap_audience
    MILOS_ADMIN_GROUP       = var.admin_group
    MILOS_SCHEDULER_SA      = google_service_account.scheduler.email
  }
}

# --- state ------------------------------------------------------------------------

resource "google_firestore_database" "default" {
  project                           = var.project
  name                              = "(default)"
  location_id                       = var.firestore_location
  type                              = "FIRESTORE_NATIVE"
  delete_protection_state           = "DELETE_PROTECTION_ENABLED"
  point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_ENABLED"
}

resource "google_firestore_backup_schedule" "weekly" {
  project   = var.project
  database  = google_firestore_database.default.name
  retention = "2419200s" # 28 days

  weekly_recurrence {
    day = "SUNDAY"
  }
}

# Composite indexes for the queries the service runs; equality-only or
# single-field queries need none.
#
# `GET /v1/sessions` lists an operator's sessions newest first.
resource "google_firestore_index" "sessions_by_operator" {
  project    = var.project
  database   = google_firestore_database.default.name
  collection = "sessions"

  fields {
    field_path = "operator"
    order      = "ASCENDING"
  }
  fields {
    field_path = "created_at"
    order      = "DESCENDING"
  }
}

resource "google_firestore_index" "sessions_by_approver" {
  project    = var.project
  database   = google_firestore_database.default.name
  collection = "sessions"

  fields {
    field_path   = "approvers"
    array_config = "CONTAINS"
  }
  fields {
    field_path = "created_at"
    order      = "DESCENDING"
  }
}



resource "google_storage_bucket" "snapshots" {
  project                     = var.project
  name                        = "${var.project}-snapshots"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age            = var.snapshot_retention_days
      matches_prefix = ["sessions/"]
    }
  }
}

# --- images and packages ----------------------------------------------------------

resource "google_artifact_registry_repository" "images" {
  project       = var.project
  location      = var.region
  repository_id = var.name
  format        = "DOCKER"
  description   = "milos images, built by CI"
}

# Cloud Build runs as this identity (`gcloud builds submit --service-account`),
# staging the source in the bucket below (`--gcs-source-staging-dir`).
# New organizations grant the Compute default account nothing, so the build
# needs its own: read the source upload, push the image, write its log.
resource "google_service_account" "build" {
  project      = var.project
  account_id   = "${var.name}-build"
  display_name = "milos image builds"
}

resource "google_project_iam_member" "build" {
  for_each = toset(["roles/logging.logWriter", "roles/artifactregistry.writer"])

  project = var.project
  role    = each.value
  member  = "serviceAccount:${google_service_account.build.email}"
}

resource "google_storage_bucket" "build_source" {
  project                     = var.project
  name                        = "${var.project}-build-source" # gcloud builds submit --gcs-source-staging-dir
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true # only source uploads live here

  lifecycle_rule {
    condition { age = 7 }
    action { type = "Delete" }
  }
}

resource "google_storage_bucket_iam_member" "build_source" {
  # Cloud Build checks the bucket itself before reading the upload.
  for_each = toset(["roles/storage.objectViewer", "roles/storage.legacyBucketReader"])

  bucket = google_storage_bucket.build_source.name
  role   = each.value
  member = "serviceAccount:${google_service_account.build.email}"
}

# Cloud Run in other projects (egress) runs the same image.
resource "google_artifact_registry_repository_iam_member" "image_pullers" {
  for_each = toset(var.image_puller_project_numbers)

  project    = var.project
  location   = var.region
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:service-${each.value}@serverless-robot-prod.iam.gserviceaccount.com"
}

# Packages reach the sandbox only through these remotes (Private Google Access
# to pkg.dev); the network has no other route upstream.
resource "google_artifact_registry_repository" "pypi" {
  project       = var.project
  location      = var.region
  repository_id = "${var.name}-pypi"
  format        = "PYTHON"
  mode          = "REMOTE_REPOSITORY"
  description   = "PyPI proxy for runners"

  remote_repository_config {
    python_repository {
      public_repository = "PYPI"
    }
  }
}

resource "google_artifact_registry_repository" "npm" {
  project       = var.project
  location      = var.region
  repository_id = "${var.name}-npm"
  format        = "NPM"
  mode          = "REMOTE_REPOSITORY"
  description   = "npm proxy for runners"

  remote_repository_config {
    npm_repository {
      public_repository = "NPMJS"
    }
  }
}

# --- secrets ----------------------------------------------------------------------

resource "random_password" "token_key" {
  length  = 48
  special = false
}

resource "google_secret_manager_secret" "token_key" {
  project   = var.project
  secret_id = "${var.name}-token-key"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "token_key" {
  secret      = google_secret_manager_secret.token_key.id
  secret_data = random_password.token_key.result
}

# --- identities -------------------------------------------------------------------

resource "google_service_account" "api" {
  project      = var.project
  account_id   = "${var.name}-api"
  display_name = "milos API"
}

resource "google_service_account" "scheduler" {
  project      = var.project
  account_id   = "${var.name}-scheduler"
  display_name = "milos scheduler"
}

resource "google_service_account" "connector" {
  project      = var.project
  account_id   = "${var.name}-connector-internal"
  display_name = "milos internal connector"
}

resource "google_service_account" "runner" {
  for_each = toset(var.agent_ids)

  project      = var.project
  account_id   = "${var.name}-runner-${each.value}"
  display_name = "milos runner for agent ${each.value}"
}

# API: Firestore, launching jobs with overrides, audit logging, the token key.
resource "google_project_iam_member" "api" {
  for_each = toset(["roles/datastore.user", "roles/logging.logWriter"])

  project = var.project
  role    = each.value
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "api_token_key" {
  project   = var.project
  secret_id = google_secret_manager_secret.token_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_cloud_run_v2_job_iam_member" "api_runs_jobs" {
  for_each = toset(var.agent_ids)

  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_job.runner[each.value].name
  role     = "roles/run.jobsExecutorWithOverrides"
  member   = "serviceAccount:${google_service_account.api.email}"
}

# Runners: Vertex AI, the snapshot bucket, the internal API, package remotes.
# Nothing else: no Firestore, no secrets, no evidence.
resource "google_project_iam_member" "runner_vertex" {
  for_each = toset(var.agent_ids)

  project = var.project
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.runner[each.value].email}"
}

resource "google_storage_bucket_iam_member" "runner_snapshots" {
  for_each = toset(var.agent_ids)

  bucket = google_storage_bucket.snapshots.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.runner[each.value].email}"
}

resource "google_artifact_registry_repository_iam_member" "runner_packages" {
  for_each = {
    for pair in setproduct(var.agent_ids, ["pypi", "npm"]) : "${pair[0]}/${pair[1]}" => pair
  }

  project    = var.project
  location   = var.region
  repository = each.value[1] == "pypi" ? google_artifact_registry_repository.pypi.name : google_artifact_registry_repository.npm.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.runner[each.value[0]].email}"
}

# --- API, public: behind IAP -------------------------------------------------------

resource "google_project_service_identity" "iap" {
  provider = google-beta
  project  = var.project
  service  = "iap.googleapis.com"
}

resource "google_cloud_run_v2_service" "public" {
  project  = var.project
  name     = local.public_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  # IAP terminates every request: only identities granted
  # roles/iap.httpsResourceAccessor below reach the container, and each request
  # carries a signed assertion the API verifies.
  iap_enabled = true

  template {
    service_account = google_service_account.api.email

    containers {
      image = var.image
      args  = ["serve", "api"]

      dynamic "env" {
        for_each = merge(local.api_env, { MILOS_API_ROLE = "public" })
        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name = "MILOS_TOKEN_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.token_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.api_token_key]
}

resource "google_cloud_run_v2_service_iam_member" "iap_invokes_public" {
  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.public.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_project_service_identity.iap.email}"
}

# Everyone allowed through IAP: the users group plus the operator service
# accounts (CLI). Keyed by a stable name so adding one never moves another.
locals {
  iap_accessors = merge(
    { group = "group:${var.users_group}" },
    { for sa in var.operator_service_accounts : "sa/${sa}" => "serviceAccount:${sa}" },
  )
}

resource "google_iap_web_cloud_run_service_iam_member" "users" {
  for_each = local.iap_accessors

  project                = var.project
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.public.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}

# --- API, internal: runners, connectors, scheduler ---------------------------------

resource "google_cloud_run_v2_service" "internal" {
  project  = var.project
  name     = local.internal_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = google_service_account.api.email

    containers {
      image = var.image
      args  = ["serve", "api"]

      dynamic "env" {
        for_each = merge(local.api_env, { MILOS_API_ROLE = "internal" })
        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name = "MILOS_TOKEN_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.token_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.api_token_key]
}

# Keyed by a stable name, not by email or position: the emails are unknown at
# plan time on a fresh project, and a positional key would re-create every
# binding after the one that moved when an agent is added.
locals {
  internal_invokers = merge(
    {
      scheduler = google_service_account.scheduler.email
      connector = google_service_account.connector.email
    },
    { for id, sa in google_service_account.runner : "runner/${id}" => sa.email },
    { for name, email in var.extra_internal_invokers : "egress/${name}" => email },
  )
}

resource "google_cloud_run_v2_service_iam_member" "internal_invokers" {
  for_each = local.internal_invokers

  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.internal.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value}"
}

# --- development only: the Anthropic API key for runners ---------------------------
# Terraform creates the secret; a person adds the version:
#   gcloud secrets versions add anthropic-api-key --project <runtime> --data-file=-

resource "google_secret_manager_secret" "anthropic_api_key" {
  count     = var.direct_anthropic_api ? 1 : 0
  project   = var.project
  secret_id = "anthropic-api-key"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "runner_anthropic_api_key" {
  for_each = var.direct_anthropic_api ? toset(var.agent_ids) : toset([])

  project   = var.project
  secret_id = google_secret_manager_secret.anthropic_api_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runner[each.value].email}"
}

# --- runner jobs: one per agent, each under its own identity ------------------------

resource "google_cloud_run_v2_job" "runner" {
  for_each = toset(var.agent_ids)

  project  = var.project
  name     = "${var.name}-runner-${each.value}"
  location = var.region

  template {
    template {
      service_account = google_service_account.runner[each.value].email
      max_retries     = 0 # a dead run is restarted by inspection, with a new lease
      timeout         = var.runner_timeout

      containers {
        image   = var.image
        command = ["python", "-m", "milos.runner"]

        resources {
          limits = {
            cpu    = "2"
            memory = "4Gi"
          }
        }

        dynamic "env" {
          for_each = local.runner_env
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = var.direct_anthropic_api ? [1] : []
          content {
            name = "ANTHROPIC_API_KEY"
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.anthropic_api_key[0].secret_id
                version = "latest"
              }
            }
          }
        }
      }

      vpc_access {
        egress = "ALL_TRAFFIC"
        network_interfaces {
          network    = var.network_id
          subnetwork = var.subnet_id
        }
      }
    }
  }
}

# --- internal connector: data-side tools, no NAT, no secrets -----------------------

resource "google_cloud_run_v2_service" "connector" {
  project  = var.project
  name     = "${var.name}-connector-internal"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = google_service_account.connector.email

    containers {
      image = var.image
      args  = ["serve", "connector", "--name", "internal"]

      env {
        name  = "MILOS_INTERNAL_API_URL"
        value = local.internal_url
      }

      dynamic "env" {
        for_each = var.data_bucket == null ? [] : [var.data_bucket]
        content {
          name  = "MILOS_DATA_BUCKET"
          value = env.value
        }
      }
    }

    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = var.network_id
        subnetwork = var.subnet_id
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "runners_invoke_connector" {
  for_each = toset(var.agent_ids)

  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.connector.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.runner[each.value].email}"
}

# --- scheduler: inspection every minute, plus unattended sessions --------------------

resource "google_cloud_scheduler_job" "inspect" {
  project     = var.project
  region      = var.region
  name        = "${var.name}-inspect"
  description = "Expire approvals, restart stalled runs"
  schedule    = "* * * * *"

  http_target {
    uri         = "${local.internal_url}/internal/inspect"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = local.internal_url
    }
  }
}

resource "google_cloud_scheduler_job" "sessions" {
  for_each = var.schedules

  project     = var.project
  region      = var.region
  name        = "${var.name}-schedule-${each.key}"
  description = "Unattended session for agent ${each.value.agent_id}"
  schedule    = each.value.cron
  time_zone   = each.value.time_zone

  http_target {
    uri         = "${local.internal_url}/internal/sessions"
    http_method = "POST"
    headers     = { "Content-Type" = "application/json" }
    # The API builds the idempotency key from X-CloudScheduler-JobName and
    # X-CloudScheduler-ScheduleTime, so a retried delivery creates no second session.
    body = base64encode(jsonencode({
      agent_id  = each.value.agent_id
      message   = each.value.message
      approvers = each.value.approvers
    }))

    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = local.internal_url
    }
  }
}

# --- alerts -------------------------------------------------------------------------

resource "google_logging_metric" "denied" {
  project = var.project
  name    = "${var.name}/tool_requests_denied"
  filter  = "logName=\"projects/${var.project}/logs/milos-audit\" AND jsonPayload.outcome=\"deny\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_notification_channel" "email" {
  count = var.alert_email == null ? 0 : 1

  project      = var.project
  display_name = "milos owners"
  type         = "email"
  labels       = { email_address = var.alert_email }
}

resource "google_monitoring_alert_policy" "denied_burst" {
  count = var.alert_email == null ? 0 : 1

  project      = var.project
  display_name = "milos: burst of denied tool requests"
  combiner     = "OR"

  conditions {
    display_name = "more than 10 denials in 5 minutes"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.denied.name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 10
      duration        = "0s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email[0].id]
}
