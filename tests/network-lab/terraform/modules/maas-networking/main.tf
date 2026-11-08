terraform {
  required_version = ">= 1.7"
  required_providers {
    maas = { source = "canonical/maas", version = "2.10.0-networklab.1" }
    lxd  = { source = "terraform-lxd/lxd", version = "3.1.1" }
  }
}
variable "api_url" { type = string }
variable "api_key_path" { type = string }
variable "ca_certificate_path" { type = string }
variable "project" { type = string }
variable "management_cidr" { type = string }
variable "management_subnet" { type = string }
variable "management_vlan" { type = string }
variable "fast_fabrics" { type = map(string) }
variable "role_vlans" { type = map(string) }
variable "roles_path" { type = string }
variable "machine_ids" { type = map(string) }
variable "machines" {
  type = map(object({ name = string, pxe_mac = string, macs = map(string), rack = number }))
}
provider "maas" {
  api_url             = var.api_url
  api_key             = sensitive(trimspace(file(var.api_key_path)))
  tls_ca_cert_path    = var.ca_certificate_path
  installation_method = "snap"
}
provider "lxd" {
  remote {
    name    = "local"
    address = "unix://"
  }
}
locals {
  workers = { for name, machine in var.machines : name => machine if name != "juju" }
  roles   = { for role in jsondecode(file(var.roles_path)).roles : role.id => role }
  interfaces = merge([for name, machine in local.workers : {
    for nic, mac in machine.macs : "${name}-${nic}" => {
      machine = name, nic = nic, mac = mac, carrier = "r${machine.rack}-${nic}"
    }
  }]...)
  children = { for item in setproduct(keys(local.workers), ["pf0", "pf1"], keys(local.roles)) : join("-", item) => {
    machine = item[0], nic = item[1], role = item[2], vid = local.roles[item[2]].vlan,
    carrier = "r${local.workers[item[0]].rack}-${item[1]}"
  } }
}
data "lxd_instance" "worker" {
  for_each = local.workers
  name     = each.key
  project  = var.project
  lifecycle {
    postcondition {
      condition = self.type == "virtual-machine" && self.config["user.maas-bird-owner"] == "network-lab" && self.config["limits.cpu"] == "1" && self.config["limits.memory"] == "4GiB" && alltrue([
        for nic, mac in each.value.macs : self.devices[nic].type == "nic" && self.devices[nic].properties["hwaddr"] == mac
      ])
      error_message = "Only the owned capped worker and its recorded NIC MACs may be configured."
    }
  }
}
data "maas_vlan" "fast_parent" {
  for_each = var.fast_fabrics
  fabric   = each.value
  vlan     = "0"
}
import {
  for_each = local.interfaces
  to       = maas_network_interface_physical.nic[each.key]
  id       = "${var.machine_ids[each.value.machine]}/${each.value.mac}"
}
resource "maas_network_interface_physical" "nic" {
  for_each    = local.interfaces
  machine     = var.machine_ids[each.value.machine]
  mac_address = each.value.mac
  mtu         = 1500
  vlan        = startswith(each.value.nic, "mgmt") ? tonumber(var.management_vlan) : tonumber(data.maas_vlan.fast_parent[each.value.carrier].id)
  depends_on  = [data.lxd_instance.worker]
}
resource "maas_network_interface_bond" "management" {
  for_each    = local.workers
  machine     = var.machine_ids[each.key]
  name        = "bond-mgmt"
  parents     = [maas_network_interface_physical.nic["${each.key}-mgmt0"].name, maas_network_interface_physical.nic["${each.key}-mgmt1"].name]
  mac_address = each.value.pxe_mac
  vlan        = tonumber(var.management_vlan)
  bond_mode   = "active-backup"
  bond_miimon = 100
  mtu         = 1500
  accept_ra   = false
}
resource "maas_network_interface_link" "management" {
  for_each          = local.workers
  machine           = var.machine_ids[each.key]
  network_interface = maas_network_interface_bond.management[each.key].name
  subnet            = var.management_subnet
  mode              = "STATIC"
  ip_address        = cidrhost(var.management_cidr, 40 + index(["probe", "probe-alt", "probe-r2a", "probe-r2b"], each.key) * 10)
  default_gateway   = true
}
resource "maas_network_interface_vlan" "role" {
  for_each  = local.children
  machine   = var.machine_ids[each.value.machine]
  parent    = maas_network_interface_physical.nic["${each.value.machine}-${each.value.nic}"].name
  fabric    = var.fast_fabrics[each.value.carrier]
  vlan      = tonumber(var.role_vlans["${each.value.carrier}-${each.value.role}"])
  name      = "${maas_network_interface_physical.nic["${each.value.machine}-${each.value.nic}"].name}.${each.value.vid}"
  mtu       = 1500
  accept_ra = false
}
resource "maas_tag" "rack" {
  for_each = toset(["1", "2"])
  name     = "rack-r${each.key}"
  machines = [for name, machine in local.workers : var.machine_ids[name] if tostring(machine.rack) == each.key]
}
output "workers" {
  value = { for name, machine in local.workers : name => {
    system_id     = var.machine_ids[name]
    system_uuid   = data.lxd_instance.worker[name].config["volatile.uuid"]
    rack          = machine.rack
    management_ip = maas_network_interface_link.management[name].ip_address
    interfaces    = { for nic, mac in machine.macs : nic => { name = maas_network_interface_physical.nic["${name}-${nic}"].name, mac = mac } }
    vlans         = { for key, child in local.children : key => { name = maas_network_interface_vlan.role[key].name, role = child.role, nic = child.nic, vid = child.vid } if child.machine == name }
  } }
}
