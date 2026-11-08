terraform {
  required_version = ">= 1.11"
  required_providers {
    juju   = { source = "juju/juju", version = "2.4.1" }
    random = { source = "hashicorp/random", version = "3.9.1" }
  }
}
variable "controller_addresses" { type = list(string) }
variable "controller_ca" { type = string }
variable "controller_username" { type = string }
variable "controller_password" {
  type      = string
  sensitive = true
}
variable "model_uuid" { type = string }
variable "maas_api_key_path" { type = string }
variable "maas_ca_path" { type = string }
variable "roles_path" { type = string }
variable "workers" { type = map(object({ rack = number })) }
variable "bgp_credential_generation" {
  type    = number
  default = 1
}
variable "maas_credential_generation" {
  type    = number
  default = 1
}
provider "juju" {
  controller_addresses = join(",", var.controller_addresses)
  ca_certificate       = var.controller_ca
  username             = var.controller_username
  password             = var.controller_password
}
locals {
  roles = jsondecode(file(var.roles_path)).roles
  sessions = merge([for name, worker in var.workers : {
    for pair in setproduct(local.roles, ["a", "b"]) : "${name}-${pair[0].id == "general" ? "gp" : pair[0].id}-r${worker.rack}${pair[1]}" => {
      host = name, role = pair[0].id, peer = "r${worker.rack}${pair[1]}"
    }
  }]...)
}
resource "random_password" "bgp" {
  for_each = local.sessions
  length   = 32
  upper    = false
  lower    = true
  numeric  = true
  special  = false
  keepers  = { generation = tostring(var.bgp_credential_generation) }
}
resource "juju_secret" "maas" {
  model_uuid       = var.model_uuid
  name             = "maas-api"
  value_wo         = { api-key = sensitive(trimspace(file(var.maas_api_key_path))) }
  value_wo_version = var.maas_credential_generation
}
resource "juju_secret" "ca" {
  model_uuid       = var.model_uuid
  name             = "maas-ca"
  value_wo         = { ca-cert = file(var.maas_ca_path) }
  value_wo_version = var.maas_credential_generation
}
resource "juju_secret" "bgp" {
  for_each         = var.workers
  model_uuid       = var.model_uuid
  name             = "bgp-${each.key}"
  value_wo         = { for id, session in local.sessions : id => random_password.bgp[id].result if session.host == each.key }
  value_wo_version = var.bgp_credential_generation
}
output "maas_secret_uri" { value = juju_secret.maas.secret_uri }
output "maas_ca_secret_uri" { value = juju_secret.ca.secret_uri }
output "bgp_secret_uris" { value = { for name, secret in juju_secret.bgp : name => secret.secret_uri } }
output "secret_ids" {
  value = { maas = juju_secret.maas.secret_id, ca = juju_secret.ca.secret_id, bgp = { for name, secret in juju_secret.bgp : name => secret.secret_id } }
}
output "bgp_passwords" {
  value     = { for name, password in random_password.bgp : name => password.result }
  sensitive = true
}

resource "random_password" "gateway" {
  for_each = toset(["r1", "r2"])
  length   = 32
  upper    = false
  lower    = true
  numeric  = true
  special  = false
  keepers  = { generation = tostring(var.bgp_credential_generation) }
}
output "gateway_passwords" {
  value     = { for name, password in random_password.gateway : name => password.result }
  sensitive = true
}
