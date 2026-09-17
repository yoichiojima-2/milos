variable "project" {
  type = string
}

variable "region" {
  type = string
}

variable "classification" {
  description = "C1, C2 or C3. Recorded as a label on every resource."
  type        = string
}

variable "retention_days" {
  type = number
}

variable "reader_service_accounts" {
  description = "Name -> identity allowed to read (normally only the internal connector)."
  type        = map(string)
  default     = {}
}

variable "datasets" {
  description = "BigQuery datasets to create, with provenance."
  type = map(object({
    description = string
    source      = string
  }))
  default = {}
}

variable "workspaces" {
  description = "Agent id -> its workspace identity and the shared datasets (keys of var.datasets) it may read. Each gets a dataset agent_<id>."
  type = map(object({
    service_account = string
    datasets        = optional(list(string), [])
  }))
  default = {}
}
