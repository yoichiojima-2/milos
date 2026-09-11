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
  default     = null
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

variable "users" {
  description = "Individual Google accounts allowed through IAP. Agent definitions must also allow them."
  type        = list(string)
  default     = []
}
