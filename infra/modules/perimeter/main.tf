# VPC Service Controls: one perimeter around every project.
#
# The perimeter stops Google-managed services (Firestore, GCS, Vertex AI,
# Secret Manager, ...) from being reached from outside the projects, and from
# moving data to projects outside. It does not block the public internet;
# that is the network modules' job. Start in dry-run: violations are logged,
# nothing is blocked, and the log is the checklist before enforcing.
#
# Requires an Access Context Manager policy at the organization; creating one
# is an org-level action and is therefore an input here, not a resource.

resource "google_access_context_manager_access_level" "admins" {
  parent = "accessPolicies/${var.access_policy_id}"
  name   = "accessPolicies/${var.access_policy_id}/accessLevels/${var.name}_admins"
  title  = "${var.name} admins"

  basic {
    conditions {
      members = var.admin_members
    }
  }
}

resource "google_access_context_manager_service_perimeter" "this" {
  parent         = "accessPolicies/${var.access_policy_id}"
  name           = "accessPolicies/${var.access_policy_id}/servicePerimeters/${var.name}"
  title          = var.name
  perimeter_type = "PERIMETER_TYPE_REGULAR"

  use_explicit_dry_run_spec = var.dry_run

  dynamic "spec" {
    for_each = var.dry_run ? [1] : []
    content {
      resources           = [for n in var.project_numbers : "projects/${n}"]
      restricted_services = var.restricted_services
      access_levels       = [google_access_context_manager_access_level.admins.name]
    }
  }

  dynamic "status" {
    for_each = var.dry_run ? [] : [1]
    content {
      resources           = [for n in var.project_numbers : "projects/${n}"]
      restricted_services = var.restricted_services
      access_levels       = [google_access_context_manager_access_level.admins.name]
    }
  }
}
