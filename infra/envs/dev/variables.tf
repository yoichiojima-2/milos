variable "folder_id" {
  description = "Numeric folder id the environment's projects are created under."
  type        = string
}

variable "billing_account" {
  type = string
}

variable "region" {
  type    = string
  default = "asia-northeast1"
}

variable "vertex_region" {
  description = "Where Claude is served on Vertex AI for this deployment."
  type        = string
  default     = "us-east5"
}

variable "image" {
  description = "Image every service and job runs, e.g. <region>-docker.pkg.dev/<runtime project>/milos/milos:<sha>."
  type        = string
}

variable "agent_ids" {
  description = "Agents with definitions under agents/. Each gets a runner job and identity."
  type        = list(string)
}

variable "users_group" {
  description = "Google group allowed through IAP to the public API."
  type        = string
}

variable "iap_audience" {
  description = "Expected audience of IAP assertions on the public service."
  type        = string
}

variable "allowed_fqdns" {
  description = "SaaS hosts the egress connector may reach."
  type        = list(string)
  default     = []
}

variable "egress_secrets" {
  description = "Secret id -> env var name for the egress connector."
  type        = map(string)
  default     = {}
}

variable "schedules" {
  description = "Unattended sessions."
  type = map(object({
    agent_id  = string
    message   = string
    cron      = string
    time_zone = optional(string, "Asia/Tokyo")
    approvers = optional(list(string), [])
  }))
  default = {}
}

variable "alert_email" {
  type    = string
  default = null
}

variable "access_policy_id" {
  description = "Access Context Manager policy id; null skips the perimeter."
  type        = string
  default     = null
}

variable "perimeter_admins" {
  type    = list(string)
  default = []
}
