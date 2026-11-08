terraform {
  required_version = ">= 1.6"
  required_providers {
    lxd = {
      source  = "terraform-lxd/lxd"
      version = "3.1.1"
    }
  }
}

provider "lxd" {
  remote {
    name    = "local"
    address = "unix://"
  }
}

variable "project_name" {
  type    = string
  default = "maas-bird-network-lab-tf"
}

variable "prefix" {
  type    = string
  default = "nlab-tf"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,6}$", var.prefix))
    error_message = "The prefix must keep Linux interface names within 15 characters."
  }
}

variable "management_cidr" {
  type    = string
  default = "10.203.241.1/24"
}

variable "enable_management_access" {
  type        = bool
  default     = false
  description = "Create separate downstream carriers for two management access switches."
}

variable "core_mtu" {
  type    = number
  default = 1600
  validation {
    condition     = var.core_mtu >= 1550 && var.core_mtu <= 9000
    error_message = "The routed core needs headroom for a 1500-byte provider packet plus VXLAN."
  }
}

variable "mtu" {
  type    = number
  default = 1500
}

locals {
  tors = ["r1a", "r1b", "r2a", "r2b"]
  fast = { for item in setproduct([1, 2], [0, 1]) : "r${item[0]}-pf${item[1]}" => item }
  core = { for item in setproduct(local.tors, [0, 1]) : "${item[0]}-s${item[1]}" => item }
  isolated = merge(
    { for name in keys(local.fast) : name => "Rack/PF VLAN trunk ${name}" },
    { for name in keys(local.core) : name => "Routed spine/ToR link ${name}" },
  )
}

resource "lxd_project" "lab" {
  name        = var.project_name
  description = "Terraform/Juju MAAS and FRR network laboratory"
  config = {
    "features.images"          = "false"
    "features.profiles"        = "true"
    "features.networks"        = "false"
    "features.storage.volumes" = "true"
    "limits.cpu"               = "16"
    "limits.memory"            = "56GiB"
    "user.maas-bird-owner"     = "network-lab"
  }
}

resource "lxd_network" "management" {
  project     = "default"
  name        = "${var.prefix}-mgmt"
  type        = "bridge"
  description = "MAAS-owned DHCP and active-backup management bonds"
  config = {
    "bridge.driver"        = "openvswitch"
    "bridge.mtu"           = tostring(var.mtu)
    "ipv4.address"         = var.management_cidr
    "ipv4.nat"             = "true"
    "ipv4.dhcp"            = "false"
    "ipv6.address"         = "none"
    "dns.mode"             = "none"
    "user.maas-bird-owner" = "network-lab"
  }
}

resource "lxd_network" "management_access" {
  for_each    = var.enable_management_access ? toset(["a", "b"]) : toset([])
  project     = "default"
  name        = "${var.prefix}-m${each.key}"
  type        = "bridge"
  description = "Management access ${each.key} downstream carrier"
  config = {
    "bridge.driver"        = "openvswitch"
    "bridge.mtu"           = tostring(var.mtu)
    "ipv4.address"         = "none"
    "ipv4.dhcp"            = "false"
    "ipv6.address"         = "none"
    "dns.mode"             = "none"
    "user.maas-bird-owner" = "network-lab"
  }
}

resource "lxd_network" "isolated" {
  for_each    = local.isolated
  project     = "default"
  name        = "${var.prefix}-${each.key}"
  type        = "bridge"
  description = each.value
  config = {
    "bridge.driver"        = "openvswitch"
    "bridge.mtu"           = tostring(contains(keys(local.core), each.key) ? var.core_mtu : var.mtu)
    "ipv4.address"         = "none"
    "ipv4.dhcp"            = "false"
    "ipv6.address"         = "none"
    "dns.mode"             = "none"
    "user.maas-bird-owner" = "network-lab"
  }
}

output "project" {
  value = lxd_project.lab.name
}

output "management_network" {
  value = lxd_network.management.name
}

output "management_cidr" {
  value = var.management_cidr
}

output "fast_networks" {
  value = { for name in keys(local.fast) : name => lxd_network.isolated[name].name }
}

output "core_networks" {
  value = { for name in keys(local.core) : name => lxd_network.isolated[name].name }
}

output "core_mtu" { value = var.core_mtu }

output "management_access_networks" {
  value = { for side, network in lxd_network.management_access : side => network.name }
}
