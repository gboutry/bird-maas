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

variable "carrier" {
  type    = string
  default = "ovn"
  validation {
    condition     = contains(["ovn", "ovs"], var.carrier)
    error_message = "Carrier must be ovn or ovs."
  }
}

data "lxd_instance" "endpoint" {
  for_each = toset(["fabric", "probe", "probe-alt"])
  project  = "maas-bird-network-lab"
  name     = each.key
}

resource "lxd_network" "probe" {
  project     = "default"
  name        = "nlab-${var.carrier}-pilot"
  type        = var.carrier == "ovn" ? "ovn" : "bridge"
  description = "Temporary native Ethernet compatibility probe"
  config = merge({
    "ipv6.address"         = "none"
    "ipv4.dhcp"            = "false"
    "user.maas-bird-owner" = "network-lab"
    }, var.carrier == "ovn" ? tomap({
      "network"      = "ai-lab-up"
      "ipv4.address" = "10.203.254.1/24"
      "ipv4.nat"     = "true"
      "ipv6.dhcp"    = "false"
      }) : tomap({
      "bridge.driver" = "openvswitch"
      "ipv4.address"  = "none"
      "dns.mode"      = "none"
    })
  )
  lifecycle {
    precondition {
      condition = alltrue([for endpoint in data.lxd_instance.endpoint :
        endpoint.type == "virtual-machine" &&
        endpoint.config["user.maas-bird-owner"] == "network-lab" &&
        endpoint.config["limits.cpu"] == "2" &&
        endpoint.config["limits.memory"] == (endpoint.name == "fabric" ? "8GiB" : "4GiB")
      ])
      error_message = "The probe requires three owned, capped endpoint VMs."
    }
  }
}

resource "lxd_instance_device" "probe" {
  for_each = {
    fabric    = { device = "ovn-pilot0", mac = "02:00:00:fa:fe:01" }
    probe     = { device = "ovn-pilot1", mac = "02:00:00:fa:fe:02" }
    probe-alt = { device = "ovn-pilot2", mac = "02:00:00:fa:fe:03" }
  }
  project       = data.lxd_instance.endpoint[each.key].project
  instance_name = each.key
  name          = each.value.device
  type          = "nic"
  properties = {
    network = lxd_network.probe.name
    hwaddr  = each.value.mac
  }
}

