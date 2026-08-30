variable "project" {
  type = string
}

variable "region" {
  type    = string
  default = "asia-northeast1"
}

variable "firestore_location" {
  description = "Firestore location. Keep it a single region (data residency)."
  type        = string
  default     = "asia-northeast1"
}

variable "image" {
  description = "Runner image, e.g. {region}-docker.pkg.dev/{project}/milos/runner:latest"
  type        = string
}

variable "job_name" {
  type    = string
  default = "milos-runner"
}

variable "job_timeout" {
  description = "Max duration of one sandbox execution"
  type        = string
  default     = "3600s"
}

variable "cpu" {
  type    = string
  default = "1"
}

variable "memory" {
  type    = string
  default = "2Gi"
}

variable "vertex_region" {
  description = "CLOUD_ML_REGION for Claude on Vertex (currently only 'global' serves Claude)"
  type        = string
  default     = "global"
}

variable "model_backend" {
  description = "Where model calls go: 'vertex' (inside the project) or 'anthropic' (mounts the anthropic-api-key secret; traffic leaves GCP). The active milos policy's model_backends list must also allow it."
  type        = string
  default     = "vertex"

  validation {
    condition     = contains(["vertex", "anthropic"], var.model_backend)
    error_message = "model_backend must be 'vertex' or 'anthropic'."
  }
}

# --- data protection (ISO 27001 A.8.10 deletion) ---

variable "session_state_retention_days" {
  description = "Days after which session state objects (sessions/*/state/) are deleted from the state bucket by lifecycle rule. The source of truth for the retention schedule — mirror it in the milos policy's retention.session_state_days so evidence and enforcement agree. The Firestore half is `milos sessions purge`."
  type        = number
  default     = 30
}

variable "evidence_retention_days" {
  description = "Bucket retention period on the evidence bucket: an exported bundle cannot be modified or deleted until this old."
  type        = number
  default     = 365
}

variable "lock_evidence_retention" {
  description = "Lock the evidence bucket's retention policy. Locking is IRREVERSIBLE — nobody, project owners included, can shorten or remove the policy afterwards, and the bucket cannot be deleted while it holds objects under retention. Leave false for dev projects; production deployments claiming the immutable-evidence control should set true."
  type        = bool
  default     = false
}

variable "audit_log_retention_days" {
  description = "Retention of the dedicated audit logging bucket (Data Access logs for Firestore/GCS — the 'who read what' record app code cannot produce)"
  type        = number
  default     = 365
}

# --- network egress ---

variable "egress_control" {
  description = "Route the runner job through a Terraform-managed VPC with default-deny egress: only allowed_egress_domains and Google APIs (via Private Google Access) are reachable. Off = default (unrestricted) egress."
  type        = bool
  default     = false
}

variable "allowed_egress_domains" {
  description = "FQDNs the sandbox may reach over tcp/443 when egress_control is on. Google APIs (*.googleapis.com) are always reachable via Private Google Access and need not be listed. No wildcards — GCP FQDN firewall objects don't support them."
  type        = list(string)
  default = [
    "api.anthropic.com",     # model_backend = "anthropic"
    "statsig.anthropic.com", # SDK telemetry (harmless if unused)
  ]
}
