terraform {
  required_version = ">= 1.6"
  required_providers {
    lxd = { source = "terraform-lxd/lxd", version = "3.1.1" }
    tls = { source = "hashicorp/tls", version = "4.4.2" }
  }
}
provider "lxd" {
  remote {
    name    = "local"
    address = "unix://"
  }
}
variable "project" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]+$", var.project))
    error_message = "Use a plain LXD project name."
  }
}
variable "management_cidr" { type = string }
locals {
  controller_address = cidrhost(var.management_cidr, 10)
  host_address       = cidrhost(var.management_cidr, 1)
}
resource "tls_private_key" "power" {
  algorithm = "RSA"
  rsa_bits  = 4096
}
resource "tls_self_signed_cert" "power" {
  private_key_pem       = tls_private_key.power.private_key_pem
  validity_period_hours = 720
  allowed_uses          = ["client_auth", "digital_signature"]
  subject { common_name = "${var.project}-power" }
}
resource "lxd_trust_certificate" "power" {
  name     = "${var.project}-power"
  content  = tls_self_signed_cert.power.cert_pem
  projects = [var.project]
  type     = "client"
}
output "power_parameters" {
  sensitive = true
  value = {
    power_address = "https://${local.controller_address}:944"
    project       = var.project
    certificate   = tls_self_signed_cert.power.cert_pem
    key           = tls_private_key.power.private_key_pem
  }
  depends_on = [terraform_data.proxy_ready, lxd_trust_certificate.power]
}
