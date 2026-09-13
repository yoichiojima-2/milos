# The runtime VPC: no NAT, no route to the internet.
#
# Cloud Run uses Direct VPC egress with all traffic through this network, so
# whatever a runner does (including bash) leaves through here. Google APIs are
# reached over Private Google Access at the restricted VIP, which only serves
# services that support VPC Service Controls. Everything else is dropped by
# the firewall policy below. The only way to the internet is the egress
# project's connector, which the runner can call but cannot become.

resource "google_compute_network" "this" {
  project                 = var.project
  name                    = var.name
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
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
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

# --- DNS: everything Google resolves to the restricted VIP ---------------------

locals {
  restricted_vip = ["199.36.153.4", "199.36.153.5", "199.36.153.6", "199.36.153.7"]
  # Zones whose names are pointed at the restricted VIP. `run.app` covers the
  # internal API and connectors; `pkg.dev` the Artifact Registry remote repos.
  zones = {
    googleapis = "googleapis.com."
    run        = "run.app."
    pkg        = "pkg.dev."
  }
}

resource "google_dns_managed_zone" "private" {
  for_each = local.zones

  project    = var.project
  name       = "${var.name}-${each.key}"
  dns_name   = each.value
  visibility = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.this.id
    }
  }
}

resource "google_dns_record_set" "restricted_a" {
  project      = var.project
  managed_zone = google_dns_managed_zone.private["googleapis"].name
  name         = "restricted.googleapis.com."
  type         = "A"
  ttl          = 300
  rrdatas      = local.restricted_vip
}

resource "google_dns_record_set" "wildcards" {
  for_each = local.zones

  project      = var.project
  managed_zone = google_dns_managed_zone.private[each.key].name
  name         = "*.${each.value}"
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["restricted.googleapis.com."]
}

# --- firewall: allow the VIP and DNS, deny the rest --------------------------------

resource "google_compute_network_firewall_policy" "this" {
  project     = var.project
  name        = var.name
  description = "Runtime egress: restricted Google APIs and DNS only"
}

resource "google_compute_network_firewall_policy_association" "this" {
  project           = var.project
  name              = var.name
  firewall_policy   = google_compute_network_firewall_policy.this.name
  attachment_target = google_compute_network.this.id
}

resource "google_compute_network_firewall_policy_rule" "allow_restricted_vip" {
  project         = var.project
  firewall_policy = google_compute_network_firewall_policy.this.name
  description     = "Private Google Access, restricted VIP"
  priority        = 100
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

resource "google_compute_network_firewall_policy_rule" "allow_dns" {
  project         = var.project
  firewall_policy = google_compute_network_firewall_policy.this.name
  description     = "Cloud DNS"
  priority        = 200
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

# --- development only: internet egress for named identities -------------------------
# The design gives the sandbox no route to the internet. This block exists for
# development against the Anthropic API when Vertex AI quota is not yet granted;
# it is off unless `internet_egress_service_accounts` is set, and the
# first-deploy check "curl to the internet must fail" does not hold while it is on.

resource "google_compute_router" "dev" {
  count   = length(var.internet_egress_service_accounts) > 0 ? 1 : 0
  project = var.project
  name    = "${var.name}-dev"
  region  = var.region
  network = google_compute_network.this.id
}

resource "google_compute_router_nat" "dev" {
  count                              = length(var.internet_egress_service_accounts) > 0 ? 1 : 0
  project                            = var.project
  name                               = "${var.name}-dev"
  region                             = var.region
  router                             = google_compute_router.dev[0].name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

resource "google_compute_network_firewall_policy_rule" "dev_https_egress" {
  count                   = length(var.internet_egress_service_accounts) > 0 ? 1 : 0
  project                 = var.project
  firewall_policy         = google_compute_network_firewall_policy.this.name
  description             = "Development only: HTTPS to the internet for the listed identities"
  priority                = 300
  direction               = "EGRESS"
  action                  = "allow"
  target_service_accounts = var.internet_egress_service_accounts

  match {
    dest_ip_ranges = ["0.0.0.0/0"]
    layer4_configs {
      ip_protocol = "tcp"
      ports       = ["443"]
    }
  }
}
