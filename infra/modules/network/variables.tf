variable "project" {
  type = string
}

variable "region" {
  type = string
}

variable "name" {
  type    = string
  default = "milos-runtime"
}

variable "subnet_cidr" {
  type    = string
  default = "10.10.0.0/24"
}

variable "internet_egress_service_accounts" {
  description = "Development only: identities allowed HTTPS egress to the internet through a NAT (direct Anthropic API). Empty keeps the sandbox closed."
  type        = list(string)
  default     = []
}
