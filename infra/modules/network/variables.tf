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
