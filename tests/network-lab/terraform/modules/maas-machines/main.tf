terraform {
  required_version = ">= 1.6"
  required_providers {
    maas = { source = "canonical/maas", version = "2.10.0-networklab.1" }
  }
}
variable "api_url" { type = string }
variable "api_key_path" { type = string }
variable "ca_certificate_path" { type = string }
variable "machines" {
  type = map(object({ name = string, pxe_mac = string, macs = map(string), rack = number }))
}
variable "power_parameters" {
  type      = map(string)
  sensitive = true
}
provider "maas" {
  api_url             = var.api_url
  api_key             = sensitive(trimspace(file(var.api_key_path)))
  tls_ca_cert_path    = var.ca_certificate_path
  installation_method = "snap"
}
resource "maas_machine" "machine" {
  for_each         = var.machines
  hostname         = each.value.name
  architecture     = "amd64/generic"
  min_hwe_kernel   = "hwe-24.04"
  testing_scripts  = ["none"]
  pxe_mac_address  = each.value.pxe_mac
  power_type       = "lxd"
  power_parameters = jsonencode(merge(var.power_parameters, { instance_name = each.value.name }))
  timeouts { create = "30m" }
}
output "machine_ids" { value = { for name, machine in maas_machine.machine : name => machine.id } }
