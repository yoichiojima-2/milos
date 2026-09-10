variable "folder_id" {
  description = "Numeric id of the folder the projects live under (the department folder)."
  type        = string
}

variable "billing_account" {
  description = "Billing account id attached to every project."
  type        = string
}

variable "prefix" {
  description = "Project id prefix."
  type        = string
  default     = "milos"
}

variable "env" {
  description = "dev, stg or prod."
  type        = string
}

variable "deletion_policy" {
  description = "PREVENT in prod; DELETE lets `terraform destroy` remove dev projects."
  type        = string
  default     = "PREVENT"
}
