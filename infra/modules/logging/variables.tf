variable "project" {
  type = string
}

variable "project_number" {
  type = string
}

variable "location" {
  type = string
}

variable "folder_id" {
  description = "Folder whose audit logs are collected (include_children)."
  type        = string
}

variable "retention_days" {
  type    = number
  default = 400
}

variable "locked" {
  description = "Lock the bucket's retention. Irreversible."
  type        = bool
  default     = false
}

variable "audit_log_name" {
  type    = string
  default = "milos-audit"
}
