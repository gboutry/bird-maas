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
variable "management_network" { type = string }
variable "access_networks" {
  type = map(string)
  validation {
    condition     = toset(keys(var.access_networks)) == toset(["a", "b"]) && length(toset(values(var.access_networks))) == 2
    error_message = "Provide two distinct management access carriers named a and b."
  }
}
variable "image" {
  type    = string
  default = "12a0de07e1683f41b169af0612897eae462efdeb84322281e91724c97e32fe0d"
}
locals {
  network = {
    version = 2
    ethernets = {
      for name in ["uplink", "downlink"] : name => {
        dhcp4 = false, dhcp6 = false, accept-ra = false, link-local = [], optional = true
      }
    }
    bridges = {
      br-access = {
        interfaces = ["uplink", "downlink"], dhcp4 = false, dhcp6 = false,
        accept-ra  = false, link-local = [], optional = true,
        parameters = { stp = false, forward-delay = 0 }
      }
    }
  }
  bootstrap = {
    "cloud-init.network-config" = yamlencode(local.network)
    "cloud-init.user-data"      = "#cloud-config\n${yamlencode({ package_update = false, package_upgrade = false })}"
  }
}
resource "terraform_data" "bootstrap" { triggers_replace = local.bootstrap }
resource "lxd_instance" "access" {
  for_each = var.access_networks
  project  = var.project
  name     = "access-${each.key}"
  image    = var.image
  type     = "container"
  profiles = []
  config = merge(local.bootstrap, {
    "limits.cpu"           = "1"
    "limits.memory"        = "768MiB"
    "boot.autostart"       = "false"
    "user.maas-bird-owner" = "network-lab"
  })
  device {
    name       = "root"
    type       = "disk"
    properties = { pool = "default", path = "/", size = "4GiB" }
  }
  device {
    name       = "uplink"
    type       = "nic"
    properties = { network = var.management_network, name = "uplink" }
  }
  device {
    name       = "downlink"
    type       = "nic"
    properties = { network = each.value, name = "downlink" }
  }
  lifecycle {
    replace_triggered_by = [terraform_data.bootstrap]
    precondition {
      condition = alltrue(flatten([
        for index, name in concat([var.management_network], values(var.access_networks)) : [
          for other_index, other in concat([var.management_network], values(var.access_networks)) :
          index == other_index || (name != other && !startswith(name, "${other}-"))
        ]
      ]))
      error_message = "Management network names must be distinct and must not begin with another management network name followed by a hyphen; LXD reserves that interface namespace for bridge cleanup."
    }
  }
}
resource "terraform_data" "ready" {
  for_each         = var.access_networks
  triggers_replace = { name = lxd_instance.access[each.key].name, project = var.project }
  lifecycle { replace_triggered_by = [lxd_instance.access[each.key]] }
  provisioner "local-exec" {
    environment = {
      PROJECT  = var.project
      INSTANCE = lxd_instance.access[each.key].name
      UPSTREAM = var.management_network
      ACCESS   = each.value
    }
    command = <<-SCRIPT
      set -eu
      lxc query "/1.0/instances/$INSTANCE?project=$PROJECT" | jq -e --arg upstream "$UPSTREAM" --arg access "$ACCESS" '
        .type == "container" and .config["user.maas-bird-owner"] == "network-lab" and
        .config["limits.cpu"] == "1" and .config["limits.memory"] == "768MiB" and
        .devices.uplink.network == $upstream and .devices.downlink.network == $access'
      timeout 300 sh -c 'until lxc exec --project "$PROJECT" "$INSTANCE" -- true 2>/dev/null; do sleep 2; done'
      lxc exec --project "$PROJECT" "$INSTANCE" -- timeout 300 cloud-init status --wait
      lxc exec --project "$PROJECT" "$INSTANCE" -- sh -ceu '
        test -d /sys/class/net/br-access/bridge
        for port in uplink downlink; do
          test "$(basename "$(readlink -f /sys/class/net/$port/master)")" = br-access
          test "$(cat /sys/class/net/br-access/brif/$port/state)" = 3
        done
        for port in br-access uplink downlink; do
          test -z "$(ip -o address show dev "$port")"
        done
      '
    SCRIPT
  }
}
data "lxd_instance" "access" {
  for_each   = var.access_networks
  project    = var.project
  name       = lxd_instance.access[each.key].name
  depends_on = [terraform_data.ready]
}
output "management_access_networks" {
  value      = var.access_networks
  depends_on = [terraform_data.ready]
}
output "switches" {
  value = { for side, instance in data.lxd_instance.access : side => {
    name = instance.name, uuid = instance.config["volatile.uuid"], network = var.access_networks[side]
  } }
}
