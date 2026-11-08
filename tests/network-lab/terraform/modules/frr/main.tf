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
variable "project" { type = string }
variable "cloud_init_timeout_seconds" {
  type        = number
  default     = 300
  nullable    = false
  description = "Provisioning deadline for guest cloud-init completion; not a routing recovery deadline."
  validation {
    condition     = var.cloud_init_timeout_seconds >= 1 && var.cloud_init_timeout_seconds <= 3600 && floor(var.cloud_init_timeout_seconds) == var.cloud_init_timeout_seconds
    error_message = "Use a whole number of cloud-init timeout seconds between 1 and 3600."
  }
}
variable "cpu_allowance" {
  type        = string
  default     = null
  description = "Optional FRR-container CPU percentage; null leaves LXD's default unchanged. Not a VM setting."
  validation {
    condition     = var.cpu_allowance == null || can(regex("^(100|[1-9][0-9]?)%$", var.cpu_allowance))
    error_message = "Use an integer CPU allowance from 1% to 100%, or leave it unset."
  }
}
variable "core_mtu" {
  type    = number
  default = 1600
}
variable "management_network" { type = string }
variable "management_cidr" { type = string }
variable "core_networks" { type = map(string) }
variable "fast_networks" { type = map(string) }
variable "image" {
  type    = string
  default = "12a0de07e1683f41b169af0612897eae462efdeb84322281e91724c97e32fe0d"
}
locals {
  tors = ["r1a", "r1b", "r2a", "r2b"]
  nodes = merge(
    { for index, name in ["spine0", "spine1"] : name => {
      asn   = 65000 + index, loopback = "10.205.250.${index + 1}", management = 100 + index,
      links = { for tor in local.tors : tor => var.core_networks["${tor}-s${index}"] }
    } },
    { for index, name in local.tors : name => {
      asn   = 65101 + index, loopback = "10.205.250.${index + 11}", management = 110 + index,
      links = { for spine in [0, 1] : "spine${spine}" => var.core_networks["${name}-s${spine}"] }
    } },
  )
}
locals {
  bootstrap = { for name, node in local.nodes : name => {
    "cloud-init.network-config" = yamlencode({
      version = 2
      ethernets = merge({
        mgmt0 = { dhcp4 = false, "accept-ra" = false, "link-local" = [], addresses = ["${cidrhost(var.management_cidr, node.management)}/24"],
          routes = [{ to = "default", via = cidrhost(var.management_cidr, 1) }],
        nameservers = { addresses = ["1.1.1.1"] } }
        }, { for peer in keys(node.links) : peer => { dhcp4 = false, dhcp6 = false, optional = true, "accept-ra" = false, "link-local" = ["ipv6"] } },
      contains(local.tors, name) ? { fast0 = { dhcp4 = false, dhcp6 = false, optional = true, "accept-ra" = false, "link-local" = ["ipv6"] } } : {})
    })
    "cloud-init.user-data" = "#cloud-config\n${yamlencode({
      package_update = true
      packages       = ["ca-certificates", "curl", "gnupg", "lldpd"]
      write_files = [
        { path = "/etc/sysctl.d/90-network-lab.conf", content = "net.ipv4.ip_forward=1\nnet.ipv6.conf.all.forwarding=1\nnet.ipv4.conf.all.rp_filter=0\nnet.ipv4.conf.default.rp_filter=0\n" },
        { path = "/etc/frr/frr.conf", content = templatefile("${path.module}/frr.conf.tftpl", {
          name  = name, asn = node.asn, loopback = node.loopback,
          peers = { for peer in keys(node.links) : peer => local.nodes[peer].asn }
        }) },
        { path = "/etc/apt/preferences.d/frr", content = "Package: frr frr-pythontools\nPin: version 10.7.1-0~ubuntu24.04.1\nPin-Priority: 1001\n" },
      ]
      runcmd = [
        ["sh", "-ceu", "curl --fail --silent --show-error https://deb.frrouting.org/frr/keys.asc | gpg --dearmor > /usr/share/keyrings/frrouting.gpg; echo 'deb [signed-by=/usr/share/keyrings/frrouting.gpg] https://deb.frrouting.org/frr noble frr-stable' > /etc/apt/sources.list.d/frr.list; apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get -o Dpkg::Options::=--force-confold install -y frr=10.7.1-0~ubuntu24.04.1"],
        ["sh", "-ceu", "sed -i -e 's/^bgpd=no/bgpd=yes/' -e 's/^bfdd=no/bfdd=yes/' /etc/frr/daemons; chown frr:frr /etc/frr/frr.conf; chmod 0640 /etc/frr/frr.conf; sysctl --system; systemctl enable frr lldpd; systemctl restart frr lldpd"],
      ]
    })}"
  } }
}
resource "terraform_data" "bootstrap" {
  for_each         = local.bootstrap
  triggers_replace = each.value
}
resource "lxd_instance" "switch" {
  for_each = local.nodes
  name     = each.key
  project  = var.project
  image    = var.image
  type     = "container"
  profiles = []
  config = merge({
    "limits.cpu"           = "1"
    "limits.memory"        = "768MiB"
    "boot.autostart"       = "false"
    "user.maas-bird-owner" = "network-lab"

  }, local.bootstrap[each.key], var.cpu_allowance == null ? {} : { "limits.cpu.allowance" = var.cpu_allowance })
  lifecycle {
    replace_triggered_by = [terraform_data.bootstrap[each.key]]
  }
  device {
    name       = "root"
    type       = "disk"
    properties = { pool = "default", path = "/", size = "4GiB" }
  }
  device {
    name       = "mgmt0"
    type       = "nic"
    properties = { network = var.management_network, name = "mgmt0" }
  }
  dynamic "device" {
    for_each = each.value.links
    content {
      name       = device.key
      type       = "nic"
      properties = { network = device.value, name = device.key }
    }
  }
  dynamic "device" {
    for_each = contains(local.tors, each.key) ? [each.key] : []
    content {
      name = "fast0"
      type = "nic"
      properties = {
        network = var.fast_networks["r${substr(each.key, 1, 1)}-pf${substr(each.key, 2, 1) == "a" ? 0 : 1}"]
        name    = "fast0"
      }
    }
  }
}
output "switches" { value = { for name, node in lxd_instance.switch : name => node.name } }
