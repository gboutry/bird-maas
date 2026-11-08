terraform {
  required_version = ">= 1.6"
  required_providers {
    maas = { source = "canonical/maas", version = "2.10.0-networklab.1" }
  }
}
variable "api_url" { type = string }
variable "api_key_path" { type = string }
variable "ca_certificate_path" { type = string }
variable "project" { type = string }
variable "management_cidr" { type = string }
variable "roles_path" { type = string }
provider "maas" {
  api_url             = var.api_url
  api_key             = sensitive(trimspace(file(var.api_key_path)))
  tls_ca_cert_path    = var.ca_certificate_path
  installation_method = "snap"
}
locals {
  roles    = { for role in jsondecode(file(var.roles_path)).roles : role.id => role }
  carriers = toset(["r1-pf0", "r1-pf1", "r2-pf0", "r2-pf1"])
  vlans = { for pair in setproduct(local.carriers, keys(local.roles)) : "${pair[0]}-${pair[1]}" => {
    carrier = pair[0], role = pair[1], vid = local.roles[pair[1]].vlan
  } }
}
resource "maas_configuration" "defaults" {
  for_each = {
    enable_analytics      = "false"
    release_notifications = "false"
  }
  key   = each.key
  value = each.value
}
data "maas_boot_source" "default" {}
resource "maas_boot_source_selection" "noble" {
  boot_source = tonumber(data.maas_boot_source.default.id)
  os          = "ubuntu"
  release     = "noble"
  arches      = ["amd64"]
  subarches   = ["ga-24.04", "hwe-24.04"]
  depends_on  = [maas_configuration.defaults]
  timeouts { create = "40m" }
}
moved {
  from = maas_configuration.defaults["default_osystem"]
  to   = maas_configuration.image_defaults["default_osystem"]
}
moved {
  from = maas_configuration.defaults["default_distro_series"]
  to   = maas_configuration.image_defaults["default_distro_series"]
}
moved {
  from = maas_configuration.defaults["commissioning_distro_series"]
  to   = maas_configuration.image_defaults["commissioning_distro_series"]
}
resource "maas_configuration" "image_defaults" {
  for_each = {
    default_osystem             = "ubuntu"
    default_distro_series       = "noble"
    commissioning_distro_series = "noble"
  }
  key        = each.key
  value      = each.value
  depends_on = [maas_boot_source_selection.noble]
}
moved {
  from = maas_configuration.defaults["default_min_hwe_kernel"]
  to   = maas_configuration.minimum_kernel
}
resource "maas_configuration" "minimum_kernel" {
  key        = "default_min_hwe_kernel"
  value      = "hwe-24.04"
  depends_on = [maas_boot_source_selection.noble]
}
data "maas_subnet" "management" { cidr = cidrsubnet(var.management_cidr, 0, 0) }
data "maas_rack_controller" "maas" { hostname = "maas" }
data "maas_fabric" "management" { name = data.maas_subnet.management.fabric }
data "maas_vlan" "management" {
  fabric = data.maas_fabric.management.id
  vlan   = tostring(data.maas_subnet.management.vid)
}
import {
  to = maas_subnet.management
  id = data.maas_subnet.management.id
}
resource "maas_subnet" "management" {
  retain_on_delete = true
  cidr             = cidrsubnet(var.management_cidr, 0, 0)
  fabric           = data.maas_fabric.management.id
  vlan             = tostring(data.maas_subnet.management.vid)
  name             = "management"
  gateway_ip       = cidrhost(var.management_cidr, 1)
  dns_servers      = [cidrhost(var.management_cidr, 10), "1.1.1.1"]
}
resource "maas_subnet_ip_range" "dynamic" {
  subnet   = maas_subnet.management.id
  type     = "dynamic"
  start_ip = cidrhost(var.management_cidr, 200)
  end_ip   = cidrhost(var.management_cidr, 239)
}
resource "maas_subnet_ip_range" "infrastructure" {
  for_each = { controllers = [1, 19], gateways = [80, 89], switches = [100, 119] }
  subnet   = maas_subnet.management.id
  type     = "reserved"
  start_ip = cidrhost(var.management_cidr, each.value[0])
  end_ip   = cidrhost(var.management_cidr, each.value[1])
}
resource "terraform_data" "dhcp" {
  triggers_replace = {
    rack    = data.maas_rack_controller.maas.id
    fabric  = data.maas_subnet.management.fabric
    vid     = data.maas_subnet.management.vid
    project = var.project
  }
  depends_on = [maas_subnet_ip_range.dynamic]
  provisioner "local-exec" {
    command = "lxc exec --project \"$PROJECT\" maas -- maas lab vlan update \"$FABRIC\" \"$VID\" dhcp_on=true primary_rack=\"$RACK\" >/dev/null"
    environment = {
      PROJECT = self.triggers_replace.project
      FABRIC  = self.triggers_replace.fabric
      VID     = tostring(self.triggers_replace.vid)
      RACK    = self.triggers_replace.rack
    }
  }
  provisioner "local-exec" {
    when    = destroy
    command = "lxc exec --project \"$PROJECT\" maas -- maas lab vlan update \"$FABRIC\" \"$VID\" dhcp_on=false >/dev/null"
    environment = {
      PROJECT = self.triggers_replace.project
      FABRIC  = self.triggers_replace.fabric
      VID     = tostring(self.triggers_replace.vid)
    }
  }
}
resource "maas_fabric" "fast" {
  for_each = local.carriers
  name     = each.key
}
resource "maas_vlan" "role" {
  for_each = local.vlans
  fabric   = maas_fabric.fast[each.value.carrier].id
  vid      = each.value.vid
  name     = each.value.role
  mtu      = 1500
  dhcp_on  = false
}
output "fast_fabrics" { value = { for name, fabric in maas_fabric.fast : name => fabric.id } }
output "role_vlans" { value = { for name, vlan in maas_vlan.role : name => vlan.id } }
output "management_subnet" { value = data.maas_subnet.management.id }
output "management_fabric" { value = data.maas_subnet.management.fabric }
output "rack_controller" { value = data.maas_rack_controller.maas.id }

output "management_vlan" { value = data.maas_vlan.management.id }
