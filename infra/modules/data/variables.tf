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
  description = "Identities allowed to read (normally only the internal connector)."
  type        = list(string)
  default     = []
}

variable "datasets" {
  description = "BigQuery datasets to create, with provenance."
  type = map(object({
    description = string
    source      = string
  }))
  default = {}
}
