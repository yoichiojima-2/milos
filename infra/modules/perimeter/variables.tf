variable "access_policy_id" {
  description = "Numeric id of the organization's Access Context Manager policy."
  type        = string
}

variable "name" {
  type    = string
  default = "milos"
}

variable "project_numbers" {
  type = list(string)
}

variable "admin_members" {
  description = "Principals allowed through the perimeter from outside (user:..., serviceAccount:...)."
  type        = list(string)
  default     = []
}

variable "dry_run" {
  description = "Log violations without blocking. Turn off only after the dry-run log is clean."
  type        = bool
  default     = true
}

variable "restricted_services" {
  type = list(string)
  default = [
    "firestore.googleapis.com",
    "storage.googleapis.com",
    "aiplatform.googleapis.com",
    "secretmanager.googleapis.com",
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "logging.googleapis.com",
    "bigquery.googleapis.com",
    "iamcredentials.googleapis.com",
    "cloudscheduler.googleapis.com",
  ]
}
