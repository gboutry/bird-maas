terraform {
  required_version = ">= 1.6"
  required_providers {
    lxd = { source = "terraform-lxd/lxd", version = "3.1.1" }
  }
}
provider "lxd" {
  remote {
    name    = "local"
    address = "unix://"
  }
}
variable "project" { type = string }
variable "supports_boot_mode" {
  type        = bool
  default     = true
  description = "Whether LXD exposes instance_boot_mode; older servers use the guest Secure Boot setting."
}
variable "management_network" { type = string }
variable "fast_networks" { type = map(string) }
variable "management_access_networks" {
  type        = map(string)
  default     = {}
  description = "Optional dedicated a/b management carriers for the four workers."
  validation {
    condition     = length(var.management_access_networks) == 0 || (toset(keys(var.management_access_networks)) == toset(["a", "b"]) && length(toset(values(var.management_access_networks))) == 2)
    error_message = "Provide both distinct management access carriers, a and b, or neither."
  }
}
variable "juju_cores" {
  type    = number
  default = 2
  validation {
    condition     = var.juju_cores >= 1 && floor(var.juju_cores) == var.juju_cores
    error_message = "Juju must have at least one whole CPU."
  }
}
locals {
  workers = { for index, name in ["probe", "probe-alt", "probe-r2a", "probe-r2b"] : name => {
    index = index + 1, rack = 1 + floor(index / 2), cores = 1, memory = "4GiB"
  } }
  machines = merge(local.workers, { juju = { index = 10, rack = 0, cores = var.juju_cores, memory = "4GiB" } })
  nics = { for name, machine in local.machines : name => merge({
    mgmt0 = { network = name == "juju" ? var.management_network : lookup(var.management_access_networks, "a", var.management_network), mac = "02:00:00:fb:${format("%02x", machine.index)}:00", priority = "10" }
    mgmt1 = { network = name == "juju" ? var.management_network : lookup(var.management_access_networks, "b", var.management_network), mac = "02:00:00:fb:${format("%02x", machine.index)}:01", priority = "0" }
    }, name == "juju" ? {} : {
    pf0 = { network = var.fast_networks["r${machine.rack}-pf0"], mac = "02:00:00:fb:${format("%02x", machine.index)}:02", priority = "0" }
    pf1 = { network = var.fast_networks["r${machine.rack}-pf1"], mac = "02:00:00:fb:${format("%02x", machine.index)}:03", priority = "0" }
  }) }
}
resource "lxd_instance" "machine" {
  for_each = local.machines
  project  = var.project
  name     = each.key
  type     = "virtual-machine"
  running  = false
  profiles = []
  config = merge({
    "limits.cpu"           = tostring(each.value.cores)
    "limits.memory"        = each.value.memory
    "boot.autostart"       = "false"
    "user.maas-bird-owner" = "network-lab"
  }, var.supports_boot_mode ? { "boot.mode" = "uefi-nosecureboot" } : { "security.secureboot" = "false" })
  device {
    name       = "root"
    type       = "disk"
    properties = { pool = "default", path = "/", size = "16GiB", "boot.priority" = "0" }
  }
  dynamic "device" {
    for_each = local.nics[each.key]
    content {
      name       = device.key
      type       = "nic"
      properties = { network = device.value.network, hwaddr = device.value.mac, "boot.priority" = device.value.priority }
    }
  }
  lifecycle { ignore_changes = [running] }
}
data "lxd_instance" "machine" {
  for_each = local.machines
  project  = lxd_instance.machine[each.key].project
  name     = lxd_instance.machine[each.key].name
}
output "machines" {
  value = { for name, machine in local.machines : name => {
    name                = lxd_instance.machine[name].name
    system_uuid         = data.lxd_instance.machine[name].config["volatile.uuid"]
    pxe_mac             = local.nics[name].mgmt0.mac
    macs                = { for nic, properties in local.nics[name] : nic => properties.mac }
    rack                = machine.rack
    management_networks = { for nic in ["mgmt0", "mgmt1"] : nic => local.nics[name][nic].network }
  } }
}
