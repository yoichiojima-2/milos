# Egress: the only project that can reach the internet.
#
# Two Cloud Run services share one image and one VPC with a NAT:
#   connector-egress  SaaS tools; holds the credentials; may reach listed FQDNs only
#   web-fetch         GET of public https URLs; holds no credentials; may reach any :443
# The firewall policy targets each rule at one service account, so the two
# identities are what separates "has secrets, few hosts" from "no secrets, any
# host". Both verify every call with the internal API before acting.

resource "google_compute_network" "this" {
  project                 = var.project
  name                    = var.name
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "this" {
  project                  = var.project
  name                     = var.name
  region                   = var.region
  network                  = google_compute_network.this.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true

  log_config {
    aggregation_interval = "INTERVAL_5_MIN"
    flow_sampling        = 1.0
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_router" "this" {
  project = var.project
  name    = var.name
  region  = var.region
  network = google_compute_network.this.id
}

resource "google_compute_router_nat" "this" {
  project                            = var.project
  name                               = var.name
  region                             = var.region
  router                             = google_compute_router.this.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ALL"
  }
}

# Google APIs (the internal API in the runtime project, Secret Manager) over
# the restricted VIP, same as the runtime network.
resource "google_dns_managed_zone" "googleapis" {
  project    = var.project
  name       = "${var.name}-googleapis"
  dns_name   = "googleapis.com."
  visibility = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.this.id
    }
  }
}

resource "google_dns_record_set" "restricted_a" {
  project      = var.project
  managed_zone = google_dns_managed_zone.googleapis.name
  name         = "restricted.googleapis.com."
  type         = "A"
  ttl          = 300
  rrdatas      = ["199.36.153.4", "199.36.153.5", "199.36.153.6", "199.36.153.7"]
}

resource "google_dns_record_set" "googleapis_wildcard" {
  project      = var.project
  managed_zone = google_dns_managed_zone.googleapis.name
  name         = "*.googleapis.com."
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["restricted.googleapis.com."]
}

resource "google_dns_managed_zone" "run" {
  project    = var.project
  name       = "${var.name}-run"
  dns_name   = "run.app."
  visibility = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.this.id
    }
  }
}

resource "google_dns_record_set" "run_wildcard" {
  project      = var.project
  managed_zone = google_dns_managed_zone.run.name
  name         = "*.run.app."
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["restricted.googleapis.com."]
}

# --- identities -------------------------------------------------------------------

resource "google_service_account" "connector" {
  project      = var.project
  account_id   = "${var.name}-connector"
  display_name = "milos egress connector (SaaS, holds credentials)"
}

resource "google_service_account" "web_fetch" {
  project      = var.project
  account_id   = "${var.name}-web-fetch"
  display_name = "milos web fetch (no credentials)"
}

# --- firewall: per-identity allowlists, default deny --------------------------------

resource "google_compute_network_firewall_policy" "this" {
  project     = var.project
  name        = var.name
  description = "Egress: SaaS FQDNs for the connector, public 443 for web fetch"
}

resource "google_compute_network_firewall_policy_association" "this" {
  project           = var.project
  name              = var.name
  firewall_policy   = google_compute_network_firewall_policy.this.name
  attachment_target = google_compute_network.this.id
}

# FQDN rules match the addresses DNS returned for the name, so hosts sharing
# an address (a CDN edge) are not told apart here; the connector verifies the
# host and the TLS certificate itself.
resource "google_compute_network_firewall_policy_rule" "connector_fqdns" {
  count = length(var.allowed_fqdns) > 0 ? 1 : 0

  project                 = var.project
  firewall_policy         = google_compute_network_firewall_policy.this.name
  description             = "SaaS hosts the connector may reach"
  priority                = 100
  direction               = "EGRESS"
  action                  = "allow"
  target_service_accounts = [google_service_account.connector.email]

  match {
    dest_fqdns = var.allowed_fqdns
    layer4_configs {
      ip_protocol = "tcp"
      ports       = ["443"]
    }
  }
}

resource "google_compute_network_firewall_policy_rule" "web_fetch_public" {
  project                 = var.project
  firewall_policy         = google_compute_network_firewall_policy.this.name
  description             = "Web fetch: any public host, https only"
  priority                = 200
  direction               = "EGRESS"
  action                  = "allow"
  target_service_accounts = [google_service_account.web_fetch.email]

  match {
    dest_ip_ranges = ["0.0.0.0/0"]
    layer4_configs {
      ip_protocol = "tcp"
      ports       = ["443"]
    }
  }
}

resource "google_compute_network_firewall_policy_rule" "restricted_vip" {
  project         = var.project
  firewall_policy = google_compute_network_firewall_policy.this.name
  description     = "Private Google Access, restricted VIP"
  priority        = 300
  direction       = "EGRESS"
  action          = "allow"

  match {
    dest_ip_ranges = ["199.36.153.4/30"]
    layer4_configs {
      ip_protocol = "tcp"
      ports       = ["443"]
    }
  }
}

resource "google_compute_network_firewall_policy_rule" "dns" {
  project         = var.project
  firewall_policy = google_compute_network_firewall_policy.this.name
  description     = "Cloud DNS"
  priority        = 400
  direction       = "EGRESS"
  action          = "allow"

  match {
    dest_ip_ranges = ["35.199.192.0/19"]
    layer4_configs {
      ip_protocol = "tcp"
      ports       = ["53"]
    }
    layer4_configs {
      ip_protocol = "udp"
      ports       = ["53"]
    }
  }
}

resource "google_compute_network_firewall_policy_rule" "deny_egress" {
  project         = var.project
  firewall_policy = google_compute_network_firewall_policy.this.name
  description     = "Default deny"
  priority        = 65000
  direction       = "EGRESS"
  action          = "deny"

  match {
    dest_ip_ranges = ["0.0.0.0/0"]
    layer4_configs {
      ip_protocol = "all"
    }
  }
}

# --- secrets: SaaS credentials, readable by the connector identity only ------------

resource "google_secret_manager_secret" "saas" {
  for_each = var.secrets

  project   = var.project
  secret_id = each.key

  replication {
    auto {}
  }

  labels = { managed_by = "terraform" }
}

resource "google_secret_manager_secret_iam_member" "connector_reads" {
  for_each = var.secrets

  project   = var.project
  secret_id = google_secret_manager_secret.saas[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.connector.email}"
}

# --- services ---------------------------------------------------------------------

locals {
  services = {
    connector = {
      name    = "${var.name}-connector"
      sa      = google_service_account.connector.email
      secrets = var.secrets
    }
    web_fetch = {
      name    = "${var.name}-web-fetch"
      sa      = google_service_account.web_fetch.email
      secrets = {}
    }
  }
}

resource "google_cloud_run_v2_service" "this" {
  for_each = local.services

  project  = var.project
  name     = each.value.name
  location = var.region
  # Callers are runners in another project of the same perimeter; VPC Service
  # Controls treats them as internal.
  ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = each.value.sa

    containers {
      image = var.image
      args  = ["serve", "connector", "--name", "egress"]

      env {
        name  = "MILOS_API_URL"
        value = var.api_internal_url
      }

      dynamic "env" {
        for_each = each.value.secrets
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.saas[env.key].secret_id
              version = "latest"
            }
          }
        }
      }
    }

    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = google_compute_network.this.id
        subnetwork = google_compute_subnetwork.this.id
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.connector_reads]
}

resource "google_cloud_run_v2_service_iam_member" "runners_invoke" {
  for_each = {
    for pair in setproduct(keys(local.services), var.runner_service_accounts) :
    "${pair[0]}/${pair[1]}" => { service = pair[0], sa = pair[1] }
  }

  project  = var.project
  location = var.region
  name     = google_cloud_run_v2_service.this[each.value.service].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value.sa}"
}
