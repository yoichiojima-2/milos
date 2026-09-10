variable "project" {
  type = string
}

variable "project_number" {
  type = string
}

variable "region" {
  type = string
}

variable "name" {
  type    = string
  default = "milos-egress"
}

variable "image" {
  type = string
}

variable "subnet_cidr" {
  type    = string
  default = "10.20.0.0/24"
}

variable "api_internal_url" {
  description = "The runtime project's internal API, used to verify permissions."
  type        = string
}

variable "runner_service_accounts" {
  description = "Runner identities allowed to call the connectors."
  type        = list(string)
}

variable "allowed_fqdns" {
  description = "SaaS hosts the credentialed connector may reach. No wildcards."
  type        = list(string)
  default     = []
}

variable "secrets" {
  description = "Secret id -> environment variable name exposed to the connector. Values are set outside Terraform."
  type        = map(string)
  default     = {}
}
