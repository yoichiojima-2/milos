variable "project" {
  description = "An existing project; this root never creates or deletes a project."
  type        = string
}
variable "region" {
  type    = string
  default = "asia-northeast1"
}
variable "vertex_region" {
  type    = string
  default = "us-east5"
}
variable "image" {
  type = string
}
variable "agent_ids" {
  type    = list(string)
  default = ["analyst"]
}
variable "users" {
  type = list(string)
}
