terraform {
  required_version = ">= 1.6"
  required_providers {
    juju  = { source = "juju/juju", version = "2.4.1" }
    maas  = { source = "canonical/maas", version = "2.10.0-networklab.1" }
    local = { source = "hashicorp/local", version = "2.9.1" }
  }
}
variable "api_url" { type = string }
variable "api_key_path" { type = string }
variable "ca_certificate_path" { type = string }
variable "controller_machine_id" { type = string }
variable "client_directory" { type = string }
variable "cpu_cores" {
  type    = number
  default = 2
  validation {
    condition     = var.cpu_cores >= 1 && var.cpu_cores == floor(var.cpu_cores)
    error_message = "CPU allocation must be a positive whole number."
  }
}
provider "juju" { controller_mode = true }
provider "maas" {
  api_url             = var.api_url
  api_key             = sensitive(trimspace(file(var.api_key_path)))
  tls_ca_cert_path    = var.ca_certificate_path
  installation_method = "snap"
}
resource "maas_tag" "controller" {
  name     = "nlab-juju-controller"
  machines = [var.controller_machine_id]
}
resource "juju_controller" "lab" {
  name                  = "nlab-tf"
  agent_version         = "3.6.29"
  juju_binary           = "/snap/juju/current/bin/juju"
  bootstrap_base        = "ubuntu@24.04"
  bootstrap_constraints = { cores = tostring(var.cpu_cores), mem = "4G", "root-disk" = "16G", tags = maas_tag.controller.name }
  cloud = {
    name            = "nlab-tf-maas"
    type            = "maas"
    endpoint        = var.api_url
    auth_types      = ["oauth1"]
    ca_certificates = [file(var.ca_certificate_path)]
  }
  cloud_credential = {
    name       = "network-lab"
    auth_type  = "oauth1"
    attributes = { maas-oauth = sensitive(trimspace(file(var.api_key_path))) }
  }
  controller_model_config = {
    "cloudinit-userdata" = yamlencode({ ca_certs = { trusted = [file(var.ca_certificate_path)] } })
  }
  model_default = {
    "enable-os-upgrade"        = "false"
    "enable-os-refresh-update" = "false"
    "default-base"             = "ubuntu@24.04"
  }
  destroy_flags = { destroy_all_models = true, destroy_storage = true }
}
resource "local_sensitive_file" "controllers" {
  filename             = "${var.client_directory}-seed/controllers.yaml"
  file_permission      = "0600"
  directory_permission = "0700"
  content = yamlencode({
    "current-controller" = juju_controller.lab.name
    controllers = { (juju_controller.lab.name) = {
      "api-endpoints" = juju_controller.lab.api_addresses
      "ca-cert"       = juju_controller.lab.ca_cert
      uuid            = juju_controller.lab.controller_uuid
      cloud           = "nlab-tf-maas"
      "agent-version" = "3.6.29"
    } }
  })
}
resource "local_sensitive_file" "accounts" {
  filename             = "${var.client_directory}-seed/accounts.yaml"
  file_permission      = "0600"
  directory_permission = "0700"
  content = yamlencode({
    controllers = { (juju_controller.lab.name) = {
      user     = juju_controller.lab.username
      password = juju_controller.lab.password
    } }
  })
}
resource "terraform_data" "client" {
  input            = var.client_directory
  triggers_replace = [local_sensitive_file.controllers.id, local_sensitive_file.accounts.id]
  provisioner "local-exec" {
    command     = "set -eu; install -d -m 0700 \"$CLIENT\"; install -m 0600 \"$SEED/controllers.yaml\" \"$CLIENT/controllers.yaml\"; install -m 0600 \"$SEED/accounts.yaml\" \"$CLIENT/accounts.yaml\""
    environment = { CLIENT = self.input, SEED = "${self.input}-seed" }
  }
  provisioner "local-exec" {
    when        = destroy
    command     = "rm -f -- \"$CLIENT/controllers.yaml\" \"$CLIENT/accounts.yaml\""
    environment = { CLIENT = self.input }
  }
}
output "api_addresses" { value = juju_controller.lab.api_addresses }
output "ca_certificate" { value = juju_controller.lab.ca_cert }
output "username" { value = juju_controller.lab.username }
output "password" {
  value     = juju_controller.lab.password
  sensitive = true
}
output "client_directory" {
  value      = var.client_directory
  depends_on = [terraform_data.client]
}
output "controller_uuid" { value = juju_controller.lab.controller_uuid }
