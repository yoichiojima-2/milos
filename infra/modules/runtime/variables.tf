variable "project" {
  type = string
}

variable "project_number" {
  type = string
}

variable "region" {
  type = string
}

variable "firestore_location" {
  type = string
}

variable "vertex_region" {
  description = "Region the Claude models are served from on Vertex AI."
  type        = string
}

variable "name" {
  type    = string
  default = "milos"
}

variable "image" {
  description = "The one image every service and job runs (built by CI)."
  type        = string
}

variable "network_id" {
  type = string
}

variable "subnet_id" {
  type = string
}

variable "agent_ids" {
  description = "Agents with a published definition: one runner job and identity each."
  type        = list(string)
}

variable "users_group" {
  description = "Google group allowed through IAP to the public API."
  type        = string
}

variable "admin_group" {
  description = "Google group whose members may publish, enable and disable definitions through the API."
  type        = string
}

variable "iap_audience" {
  description = "Expected `aud` of IAP assertions for the public service. Confirm on the first deploy; see docs/operations.md."
  type        = string
}

variable "connector_urls" {
  description = "Connector name -> URL from other projects (the egress module's outputs)."
  type        = map(string)
  default     = {}
}

variable "extra_internal_invokers" {
  description = "Name -> service account outside this project allowed to call the internal API (the egress module's service_accounts output)."
  type        = map(string)
  default     = {}
}

variable "schedules" {
  description = "Unattended sessions: id -> {agent_id, message, cron, time_zone, approvers}."
  type = map(object({
    agent_id  = string
    message   = string
    cron      = string
    time_zone = optional(string, "Asia/Tokyo")
    approvers = optional(list(string), [])
  }))
  default = {}
}

variable "snapshot_retention_days" {
  type    = number
  default = 90
}

variable "runner_timeout" {
  type    = string
  default = "3600s"
}

variable "alert_email" {
  type    = string
  default = null
}


variable "operator_service_accounts" {
  description = "Service accounts allowed through IAP to the public API, for CLI use where user tokens are not accepted (Google-managed OAuth client). Agent definitions must also allow them."
  type        = list(string)
  default     = []
}

variable "image_puller_project_numbers" {
  description = "Other projects whose Cloud Run services run this image; their Cloud Run service agents may read the registry."
  type        = list(string)
  default     = []
}

variable "direct_anthropic_api" {
  description = "Development only: runners call the Anthropic API with the key in Secret Manager secret `anthropic-api-key` instead of Vertex AI. Requires internet egress on the network."
  type        = bool
  default     = false
}

variable "data_bucket" {
  description = "Bucket in the data project the internal connector reads (its list_files/read_file tools). Null registers no data tools."
  type        = string
  default     = null
}

variable "data_project" {
  description = "The data project whose BigQuery datasets the internal connector reaches (its bq_* tools). Null registers no BigQuery tools."
  type        = string
  default     = null
}

variable "workspace_agent_ids" {
  description = "Agents with a BigQuery workspace: one workspace identity each, impersonated by the internal connector."
  type        = list(string)
  default     = []
}
