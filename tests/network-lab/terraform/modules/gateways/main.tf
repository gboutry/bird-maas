terraform {
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
variable "management_cidr" { type = string }
variable "fast_networks" { type = map(string) }
variable "passwords" {
  type      = map(string)
  sensitive = true
}
variable "image" {
  type    = string
  default = "a80e9c9fd0d484dec024da3e9e264bbad4ce159b5312d6e149ef6ad532e18e43"
}
locals {
  gateways = { for rack in [1, 2] : "r${rack}" => {
    rack           = rack, name = "gateway-r${rack}", tor = "r${rack}a", asn = 4200000100 + rack
    address        = "10.205.251.${rack}", link_local = "fe80::a0:4:${80 + rack}"
    management     = cidrhost(var.management_cidr, 80 + rack)
    management_mac = "02:00:00:fb:${format("%02x", 80 + rack)}:00"
    fast_mac       = "02:00:00:fb:${format("%02x", 80 + rack)}:01"
  } }
  bootstrap = { for name, gateway in local.gateways : name => {
    "cloud-init.network-config" = yamlencode({
      version = 2
      ethernets = {
        mgmt0 = {
          match       = { macaddress = gateway.management_mac }, "set-name" = "mgmt0"
          dhcp4       = false, dhcp6 = false, "accept-ra" = false, "link-local" = []
          addresses   = ["${gateway.management}/24"]
          routes      = [{ to = "default", via = cidrhost(var.management_cidr, 1) }]
          nameservers = { addresses = ["1.1.1.1"] }
        }
        fast0 = {
          match = { macaddress = gateway.fast_mac }, "set-name" = "fast0"
          dhcp4 = false, dhcp6 = false, "accept-ra" = false, "link-local" = []
        }
        lo = {
          match  = { name = "lo" }, addresses = ["${gateway.address}/32"]
          routes = [{ to = "10.205.0.0/16", type = "unreachable", metric = 42760 }]
        }
      }
      vlans = {
        "fast0.3404" = {
          id           = 3404, link = "fast0", addresses = ["${gateway.link_local}/64"]
          "link-local" = [], "accept-ra" = false
        }
        "fast0.3405" = { id = 3405, link = "fast0", "link-local" = [], "accept-ra" = false }
      }
    })
    "cloud-init.user-data" = "#cloud-config\n${yamlencode({
      package_update = true
      packages       = ["ca-certificates", "curl", "gnupg", "jq", "openvswitch-switch", "ovn-host", "lldpd", "linux-generic-hwe-24.04"]
      write_files = [
        { path = "/etc/frr/frr.conf", permissions = "0600", content = templatefile("${path.module}/frr.conf.tftpl", {
          gateway = gateway, password = var.passwords[name]
        }) },
        { path = "/etc/apt/preferences.d/frr", content = "Package: frr frr-pythontools\nPin: version 10.7.1-0~ubuntu24.04.1\nPin-Priority: 1001\n" },
        { path = "/etc/systemd/networkd.conf.d/90-network-lab.conf", content = "[Network]\nManageForeignRoutes=no\n" },
      ]
      runcmd = [
        ["sh", "-ceu", "curl --fail --silent --show-error https://deb.frrouting.org/frr/keys.asc | gpg --dearmor > /usr/share/keyrings/frrouting.gpg; echo 'deb [signed-by=/usr/share/keyrings/frrouting.gpg] https://deb.frrouting.org/frr noble frr-stable' > /etc/apt/sources.list.d/frr.list; apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get -o Dpkg::Options::=--force-confold install -y frr=10.7.1-0~ubuntu24.04.1"],
        ["sh", "-ceu", "sed -i -e 's/^bgpd=no/bgpd=yes/' -e 's/^bfdd=no/bfdd=yes/' /etc/frr/daemons; chown frr:frr /etc/frr/frr.conf; chmod 0640 /etc/frr/frr.conf; systemctl enable frr lldpd; systemctl restart frr lldpd"],
      ]
      power_state = { mode = "reboot", timeout = 30, condition = ["test", "-f", "/var/run/reboot-required"] }
    })}"
  } }
}
resource "terraform_data" "bootstrap" {
  for_each         = local.gateways
  triggers_replace = local.bootstrap[each.key]
}
resource "lxd_instance" "gateway" {
  for_each = local.gateways
  name     = each.value.name
  project  = var.project
  type     = "virtual-machine"
  image    = var.image
  profiles = []
  config = merge(local.bootstrap[each.key], {
    "limits.cpu"           = "1", "limits.memory" = "2GiB", "boot.autostart" = "false"
    "user.maas-bird-owner" = "network-lab"
  })
  lifecycle { replace_triggered_by = [terraform_data.bootstrap[each.key]] }
  device {
    name       = "root"
    type       = "disk"
    properties = { pool = "default", path = "/", size = "16GiB" }
  }
  device {
    name       = "mgmt0"
    type       = "nic"
    properties = { network = var.management_network, hwaddr = each.value.management_mac }
  }
  device {
    name       = "fast0"
    type       = "nic"
    properties = { network = var.fast_networks["r${each.value.rack}-pf0"], hwaddr = each.value.fast_mac }
  }
}
resource "terraform_data" "ready" {
  for_each         = local.gateways
  triggers_replace = terraform_data.bootstrap[each.key].id
  depends_on       = [lxd_instance.gateway]
  provisioner "local-exec" {
    command     = <<-SCRIPT
      set -eu
      deadline=$(($(date +%s) + 1200))
      until timeout 30 lxc exec --project "$PROJECT" "$INSTANCE" -- sh -ceu '
        test -f /var/lib/cloud/instance/boot-finished
        test ! -f /var/run/reboot-required
        case "$(uname -r)" in 6.8.*) exit 1;; esac
        cloud-init status --wait
      '; do
        test "$(date +%s)" -lt "$deadline"
        sleep 5
      done
    SCRIPT
    environment = { PROJECT = var.project, INSTANCE = each.value.name }
  }
}
data "lxd_instance" "gateway" {
  for_each = local.gateways
  name     = lxd_instance.gateway[each.key].name
  project  = var.project
}
output "gateways" {
  value = { for name, gateway in local.gateways : name => merge(gateway, { system_uuid = data.lxd_instance.gateway[name].config["volatile.uuid"] }) }
}

resource "terraform_data" "provider" {
  for_each         = local.gateways
  triggers_replace = terraform_data.bootstrap[each.key].id
  depends_on       = [terraform_data.ready]
  provisioner "local-exec" {
    command     = <<-SCRIPT
      lxc exec --project "$PROJECT" "$INSTANCE" -- sh -ceu '
        ovs-vsctl --may-exist add-br br-provider -- set bridge br-provider other_config:hwaddr=02:00:00:ee:0${each.value.rack}:01 -- --may-exist add-port br-provider fast0.3405
        ip link set fast0.3405 up
        ip link set br-provider up
      '
    SCRIPT
    environment = { PROJECT = var.project, INSTANCE = each.value.name }
  }
}
