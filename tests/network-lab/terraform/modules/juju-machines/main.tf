terraform {
  required_version = ">= 1.7"
  required_providers {
    juju = { source = "juju/juju", version = "2.4.1" }
    lxd  = { source = "terraform-lxd/lxd", version = "3.1.1" }
  }
}
variable "controller_addresses" { type = list(string) }
variable "controller_ca" { type = string }
variable "controller_username" { type = string }
variable "controller_password" {
  type      = string
  sensitive = true
}
variable "maas_ca_path" { type = string }
variable "maas_api_key_path" { type = string }
variable "project" { type = string }
variable "management_arp_monitoring" {
  type    = bool
  default = false
}
variable "management_cidr" {
  type    = string
  default = "10.203.241.0/24"
  validation {
    condition     = can(cidrnetmask(var.management_cidr))
    error_message = "Management ARP monitoring requires an IPv4 management CIDR."
  }
}
variable "roles_path" { type = string }
variable "workers" {
  type = map(object({
    system_id  = string, system_uuid = string, rack = number, management_ip = string,
    interfaces = map(object({ name = string, mac = string })),
    vlans      = map(object({ name = string, role = string, nic = string, vid = number }))
  }))
}
provider "juju" {
  controller_addresses = join(",", var.controller_addresses)
  ca_certificate       = var.controller_ca
  username             = var.controller_username
  password             = var.controller_password
}
locals {
  roles      = { for role in jsondecode(file(var.roles_path)).roles : role.id => role }
  host_order = ["probe", "probe-alt", "probe-r2a", "probe-r2b"]
  # MAAS owns bond identity; upstream ARP replaces its inherited MII monitor.
  # Active validation does not require active-path ARP broadcasts on backup.
  management_bond = var.management_arp_monitoring ? {
    bonds = {
      bond-mgmt = {
        parameters = {
          mode                 = "active-backup"
          mii-monitor-interval = 0
          arp-interval         = "200ms"
          arp-ip-targets       = [cidrhost(var.management_cidr, 1)]
          arp-validate         = "active"
          fail-over-mac-policy = "none"
        }
      }
    }
  } : {}
  netplan = { for name, worker in var.workers : name => {
    network = merge(local.management_bond, {
      version = 2
      ethernets = merge({
        lo = {
          match            = { name = "lo" }, "accept-ra" = false, "link-local" = [], optional = true,
          addresses        = [for role in local.roles : "${cidrhost(role.address_block, 20 + index(local.host_order, name))}/32"],
          routes           = concat([for role in local.roles : { to = "0.0.0.0/0", type = "unreachable", table = role.table, metric = 42760 }], [{ to = "10.205.0.0/16", type = "unreachable", table = 254, metric = 42760 }]),
          "routing-policy" = [for role in local.roles : { from = "${cidrhost(role.address_block, 20 + index(local.host_order, name))}/32", table = role.table, priority = role.rule_priority }]
        }
      }, { for nic in ["pf0", "pf1"] : worker.interfaces[nic].name => { "accept-ra" = false, "link-local" = [], optional = true } })
      vlans = { for vlan in worker.vlans : vlan.name => {
        id          = vlan.vid, link = worker.interfaces[vlan.nic].name,
        "accept-ra" = false, "link-local" = [], optional = true,
        addresses   = [format("fe80::a0:%x:%x/64", local.roles[vlan.role].slot, 40 + 2 * index(local.host_order, name) + (vlan.nic == "pf0" ? 0 : 1))]
      } }
    })
  } }
  management_macs = var.management_arp_monitoring ? {
    for name, worker in var.workers : name => lower(regex("^[[:xdigit:]]{2}(?::[[:xdigit:]]{2}){5}$", worker.interfaces["mgmt0"].mac))
  } : {}
  # networkd does not update existing bond netdev monitoring on Netplan apply.
  management_arp_apply = join("\n", concat(
    ["case \"$profile\" in"],
    [for name, mac in local.management_macs : "  ${name}) expected_management_mac=${mac} ;;"],
    ["  *) echo 'Unknown management bond identity' >&2; exit 1 ;;", "esac"],
    [
      "test \"$(cat /sys/class/net/bond-mgmt/bonding/mode)\" = 'active-backup 1'",
      "test \"$(cat /sys/class/net/bond-mgmt/bonding/fail_over_mac)\" = 'none 0'",
      "test \"$(cat /sys/class/net/bond-mgmt/address)\" = \"$expected_management_mac\"",
      "management_addresses=$(ip -o -4 address show dev bond-mgmt)",
      "test -n \"$management_addresses\"",
      "if test \"$(cat /sys/class/net/bond-mgmt/bonding/miimon)\" != 0 || test \"$(cat /sys/class/net/bond-mgmt/bonding/arp_interval)\" != 200 || test \"$(cat /sys/class/net/bond-mgmt/bonding/arp_ip_target)\" != ${cidrhost(var.management_cidr, 1)} || test \"$(cat /sys/class/net/bond-mgmt/bonding/arp_validate)\" != 'active 1'; then",
      "  ip link set dev bond-mgmt type bond miimon 0 arp_interval 200 arp_ip_target ${cidrhost(var.management_cidr, 1)} arp_validate active",
      "fi",
      "test \"$(cat /sys/class/net/bond-mgmt/bonding/miimon)\" = 0",
      "test \"$(cat /sys/class/net/bond-mgmt/bonding/arp_interval)\" = 200",
      "test \"$(cat /sys/class/net/bond-mgmt/bonding/arp_ip_target)\" = ${cidrhost(var.management_cidr, 1)}",
      "test \"$(cat /sys/class/net/bond-mgmt/bonding/arp_validate)\" = 'active 1'",
      "test \"$(cat /sys/class/net/bond-mgmt/address)\" = \"$expected_management_mac\"",
      "test \"$(ip -o -4 address show dev bond-mgmt)\" = \"$management_addresses\""
    ]
  ))
  select_profile = join("\n", concat(
    ["set -eu", "identity=$(tr '[:upper:]' '[:lower:]' < /sys/class/dmi/id/product_uuid)", "case \"$identity\" in"],
    [for name, worker in var.workers : "  ${worker.system_uuid}) profile=${name} ;;"],
    ["  *) echo 'Unrecognized machine UUID; refusing network configuration' >&2; exit 1 ;;", "esac", "install -m 0600 \"/etc/network-lab/netplan/$profile.yaml\" /etc/netplan/99-network-lab-roles.yaml", "netplan generate", "netplan apply"],
    var.management_arp_monitoring ? [local.management_arp_apply] : [],
    ["install -m 0644 \"/etc/network-lab/lldp/$profile.default\" /etc/default/lldpd", "systemctl restart lldpd"]
  ))
  userdata = yamlencode({
    ca_certs    = { trusted = [file(var.maas_ca_path)] }
    packages    = ["lldpd", "openvswitch-switch", "ovn-host"]
    write_files = concat([for name, configuration in local.netplan : { path = "/etc/network-lab/netplan/${name}.yaml", permissions = "0600", content = yamlencode(configuration) }], [{ path = "/etc/systemd/networkd.conf.d/90-network-lab.conf", permissions = "0644", content = local.networkd_owner }], [for name, content in local.lldp_defaults : { path = "/etc/network-lab/lldp/${name}.default", permissions = "0600", content = content }])
    postruncmd  = [["sh", "-ceu", local.select_profile]]
  })
}
resource "juju_credential" "maas" {
  name = "network-lab"
  cloud { name = "nlab-tf-maas" }
  auth_type             = "oauth1"
  attributes            = { maas-oauth = sensitive(trimspace(file(var.maas_api_key_path))) }
  controller_credential = true
  client_credential     = false
}
resource "juju_model" "lab" {
  name = "network-lab"
  cloud { name = "nlab-tf-maas" }
  credential = juju_credential.maas.name
  config = {
    "default-base"                = "ubuntu@24.04"
    "disable-network-management"  = "true"
    "update-status-hook-interval" = "1m"
    "enable-os-upgrade"           = "false"
    "enable-os-refresh-update"    = "false"
    "cloudinit-userdata"          = local.userdata
  }
}
resource "juju_machine" "worker" {
  for_each          = var.workers
  model_uuid        = juju_model.lab.uuid
  name              = each.key
  base              = "ubuntu@24.04"
  placement         = "system-id=${each.value.system_id}"
  constraints       = "cores=1 mem=4G root-disk=16G"
  wait_for_hostname = true
  timeouts { create = "30m" }
}
resource "terraform_data" "ready" {
  for_each         = var.workers
  triggers_replace = juju_machine.worker[each.key].id
  provisioner "local-exec" {
    command     = <<-SCRIPT
      set -eu
      status=$(mktemp)
      trap 'rm -f "$status"' EXIT
      code=0
      result=$(lxc exec --project "$PROJECT" "$INSTANCE" -- timeout 600 cloud-init status --wait --format=json) || code=$?
      printf '%s\n' "$result" > "$status"
      test "$code" -eq 0 || test "$code" -eq 2
      jq -e '
        .status == "done" and .errors == [] and .["modules-final"].errors == [] and
        ((.recoverable_errors | keys) - ["WARNING"] | length == 0) and
        all(.recoverable_errors.WARNING[]?;
          startswith("Failed posting event: ") and contains("[Errno 101] Network is unreachable"))
      ' "$status" >/dev/null
      if test "$code" -eq 2; then
        echo "Bootstrap completed with MAAS early-network reporting warnings; strict cloud-init acceptance remains separate" >&2
      fi
    SCRIPT
    environment = { PROJECT = var.project, INSTANCE = each.key }
  }
}
output "model_uuid" { value = juju_model.lab.uuid }
output "machines" {
  value      = { for name, machine in juju_machine.worker : name => machine.machine_id }
  depends_on = [terraform_data.ready]
}

output "worker_configuration" {
  value = { for name, worker in var.workers : name => {
    project    = var.project
    uuid       = worker.system_uuid
    generation = juju_machine.worker[name].id
    networkd   = local.networkd_owner
    lldp       = local.lldp_defaults[name]
  } }
  depends_on = [terraform_data.ready]
}
