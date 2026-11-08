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
variable "core_mtu" {
  type    = number
  default = 1600
}
variable "switch_uuids" { type = map(string) }
variable "workers" { type = map(object({ rack = number })) }
variable "roles_path" { type = string }
variable "gateways" {
  type    = map(object({ tor = string, name = string, address = string, link_local = string, asn = number }))
  default = {}
}
variable "gateway_passwords" {
  type      = map(string)
  sensitive = true
  default   = {}
}
variable "bgp_passwords" {
  type      = map(string)
  sensitive = true
}
locals {
  tors  = ["r1a", "r1b", "r2a", "r2b"]
  hosts = ["probe", "probe-alt", "probe-r2a", "probe-r2b"]
  roles = jsondecode(file(var.roles_path)).roles
  domains = {
    general          = { vrf = "", vlan = 0, table = 0 }
    ceph-access      = { vrf = "access", vlan = 3420, table = 6200 }
    ceph-replication = { vrf = "replication", vlan = 3430, table = 6300 }
  }
  nodes = merge(
    { for i, name in ["spine0", "spine1"] : name => {
      asn = 65000 + i, loopback = "10.205.250.${i + 1}", peers = { for j, tor in local.tors : tor => 65101 + j }
    } },
    { for i, tor in local.tors : tor => {
      asn = 65101 + i, loopback = "10.205.250.${i + 11}", peers = { spine0 = 65000, spine1 = 65001 }
    } },
  )
  host_sessions = { for tor in local.tors : tor => flatten([for name, worker in var.workers : [for role in local.roles : {
    id        = "${name}-${role.id == "general" ? "gp" : role.id}-${tor}", host = name, domain = role.domain
    interface = "fast0.${role.vlan}", asn = 4200000020 + index(local.hosts, name)
    address   = format("fe80::a0:%x:%x", role.slot, 40 + 2 * index(local.hosts, name) + (endswith(tor, "a") ? 0 : 1))
    prefix    = "${cidrhost(role.address_block, 20 + index(local.hosts, name))}/32"
    imports   = concat(flatten([for other_role in local.roles : [for other in local.hosts : "${cidrhost(other_role.address_block, 20 + index(local.hosts, other))}/32" if role.domain == "general" || other != name] if other_role.domain == role.domain]), role.domain == "general" ? [for gateway in var.gateways : "${gateway.address}/32"] : [])
  }] if startswith(tor, "r${worker.rack}")]) }
  sessions = { for tor in local.tors : tor => concat(local.host_sessions[tor], [for name, gateway in var.gateways : {
    id      = "gateway-${name}", host = gateway.name, domain = "general", interface = "fast0.3404"
    asn     = gateway.asn, address = gateway.link_local, prefix = "${gateway.address}/32"
    imports = concat([for i, host in local.hosts : "10.205.10.${20 + i}/32"], [for peer in var.gateways : "${peer.address}/32"])
  } if gateway.tor == tor]) }
  core_networks = merge([for name, node in local.nodes : { for peer in keys(node.peers) : "${name}/${peer}" => {
    node    = name, path = "/etc/systemd/network/05-network-lab-${peer}.network"
    content = "[Match]\nName=${peer}\n[Link]\nMTUBytes=${var.core_mtu}\n[Network]\nLinkLocalAddressing=ipv6\nIPv6AcceptRA=no\nVLAN=${peer}.3420\nVLAN=${peer}.3430\n"
  } }]...)
  vrfs = merge([for name, node in local.nodes : { for domain in ["ceph-access", "ceph-replication"] : "${name}/${domain}" => {
    node    = name, path = "/etc/systemd/network/05-network-lab-${local.domains[domain].vrf}.netdev"
    content = "[NetDev]\nName=${local.domains[domain].vrf}\nKind=vrf\n[VRF]\nTable=${local.domains[domain].table}\n"
  } }]...)
  vlan_intents = merge(
    merge([for name, node in local.nodes : merge([for peer in keys(node.peers) : { for domain in ["ceph-access", "ceph-replication"] : "${name}/${peer}/${domain}" => {
      node = name, parent = peer, name = "${peer}.${local.domains[domain].vlan}", vid = local.domains[domain].vlan, vrf = local.domains[domain].vrf, address = ""
    } }]...)]...),
    merge([for tor in local.tors : { for role in local.roles : "${tor}/fast0/${role.id}" => {
      node = tor, parent = "fast0", name = "fast0.${role.vlan}", vid = role.vlan, vrf = local.domains[role.domain].vrf, address = "fe80::1/64"
    } }]...),
  )
  vlan_files = merge([for key, vlan in local.vlan_intents : {
    "${key}/netdev" = {
      node    = vlan.node, path = "/etc/systemd/network/05-network-lab-${vlan.name}.netdev"
      content = "[NetDev]\nName=${vlan.name}\nKind=vlan\n[VLAN]\nId=${vlan.vid}\n"
    }
    "${key}/network" = {
      node    = vlan.node, path = "/etc/systemd/network/05-network-lab-${vlan.name}.network"
      content = "[Match]\nName=${vlan.name}\n[Network]\nLinkLocalAddressing=${vlan.address == "" ? "ipv6" : "no"}\nIPv6AcceptRA=no\n${vlan.vrf == "" ? "" : "VRF=${vlan.vrf}\n"}${vlan.address == "" ? "" : "Address=${vlan.address}\n"}"
    }
  }]...)
  fast_networks = { for tor in local.tors : "${tor}/fast0" => {
    node    = tor, path = "/etc/systemd/network/05-network-lab-fast0.network"
    content = "[Match]\nName=fast0\n[Network]\nLinkLocalAddressing=ipv6\nIPv6AcceptRA=no\n${join("\n", [for role in local.roles : "VLAN=fast0.${role.vlan}"])}\nVLAN=fast0.3405\n"
  } }
  vrf_networks = merge([for name, node in local.nodes : { for domain in ["ceph-access", "ceph-replication"] : "${name}/${domain}/network" => {
    node    = name, path = "/etc/systemd/network/05-network-lab-${local.domains[domain].vrf}.network"
    content = "[Match]\nName=${local.domains[domain].vrf}\n[Network]\nLinkLocalAddressing=no\nIPv6AcceptRA=no\n"
  } }]...)
  networkd_policy = { for name, node in local.nodes : "${name}/networkd-policy" => {
    node    = name, path = "/etc/systemd/networkd.conf.d/90-network-lab.conf"
    content = "[Network]\nManageForeignRoutes=no\n"
  } }
  provider_files = merge([for tor in local.tors : {
    "${tor}/provider-bridge/netdev" = {
      node    = tor, path = "/etc/systemd/network/05-network-lab-br-provider.netdev"
      content = "[NetDev]\nName=br-provider\nKind=bridge\nMTUBytes=1500\n[Bridge]\nSTP=false\n"
    }
    "${tor}/provider-bridge/network" = {
      node    = tor, path = "/etc/systemd/network/05-network-lab-br-provider.network"
      content = "[Match]\nName=br-provider\n[Network]\nLinkLocalAddressing=no\nIPv6AcceptRA=no\n"
    }
    "${tor}/provider-vxlan/netdev" = {
      node    = tor, path = "/etc/systemd/network/05-network-lab-vx-provider.netdev"
      content = "[NetDev]\nName=vx-provider\nKind=vxlan\nMTUBytes=1500\n[VXLAN]\nVNI=3405\nLocal=${local.nodes[tor].loopback}\nDestinationPort=4789\nMacLearning=false\nIndependent=true\nIPDoNotFragment=false\n"
    }
    "${tor}/provider-vxlan/network" = {
      node    = tor, path = "/etc/systemd/network/05-network-lab-vx-provider.network"
      content = "[Match]\nName=vx-provider\n[Network]\nBridge=br-provider\nLinkLocalAddressing=no\nIPv6AcceptRA=no\n[Bridge]\nLearning=no\nNeighborSuppression=yes\n"
    }
    "${tor}/provider-vlan/netdev" = {
      node    = tor, path = "/etc/systemd/network/05-network-lab-fast0.3405.netdev"
      content = "[NetDev]\nName=fast0.3405\nKind=vlan\n[VLAN]\nId=3405\n"
    }
    "${tor}/provider-vlan/network" = {
      node    = tor, path = "/etc/systemd/network/05-network-lab-fast0.3405.network"
      content = "[Match]\nName=fast0.3405\n[Network]\nBridge=br-provider\nLinkLocalAddressing=no\nIPv6AcceptRA=no\n"
    }
  }]...)
  network_files = merge(local.core_networks, local.vrfs, local.vrf_networks, local.vlan_files, local.fast_networks, local.networkd_policy, local.provider_files)
  configurations = { for name, node in local.nodes : name => templatefile("${path.module}/frr.conf.tftpl", {
    name     = name, node = node, domains = local.domains, roles = local.roles, hosts = local.hosts
    sessions = lookup(local.sessions, name, []), passwords = merge(var.bgp_passwords, { for name, password in var.gateway_passwords : "gateway-${name}" => password }), gateway_prefixes = [for gateway in var.gateways : "${gateway.address}/32"], is_tor = contains(local.tors, name)
  }) }
}
data "lxd_instance" "switch" {
  for_each = local.nodes
  name     = each.key
  project  = var.project
  lifecycle {
    postcondition {
      condition     = self.config["user.maas-bird-owner"] == "network-lab" && self.config["volatile.uuid"] == var.switch_uuids[each.key]
      error_message = "Switch ownership or hardware identity changed."
    }
  }
}
resource "lxd_instance_file" "network" {
  for_each           = local.network_files
  instance           = data.lxd_instance.switch[each.value.node].name
  project            = var.project
  target_path        = each.value.path
  content            = each.value.content
  mode               = "0644"
  uid                = 0
  gid                = 0
  create_directories = true
}
resource "lxd_instance_file" "frr" {
  for_each           = local.nodes
  instance           = data.lxd_instance.switch[each.key].name
  project            = var.project
  target_path        = "/root/network-lab/frr.conf"
  content            = local.configurations[each.key]
  mode               = "0600"
  uid                = 0
  gid                = 0
  create_directories = true
}
resource "terraform_data" "network" {
  for_each         = local.nodes
  triggers_replace = [var.switch_uuids[each.key], sha256(jsonencode({ for key, value in local.network_files : key => value if value.node == each.key }))]
  depends_on       = [lxd_instance_file.network]
  provisioner "local-exec" {
    command     = <<-SCRIPT
      lxc exec --project "$PROJECT" "$INSTANCE" -- sh -ceu '
        systemctl enable systemd-networkd
        systemctl restart systemd-networkd
        networkctl reconfigure access replication ${join(" ", keys(each.value.peers))}${contains(local.tors, each.key) ? " fast0 br-provider vx-provider" : ""}
        systemctl restart frr
      '
    SCRIPT
    environment = { PROJECT = var.project, INSTANCE = each.key }
  }
}
resource "terraform_data" "apply" {
  for_each         = local.nodes
  triggers_replace = [var.switch_uuids[each.key], sha256(local.configurations[each.key]), sha256(jsonencode({ for key, value in local.network_files : key => value if value.node == each.key }))]
  depends_on       = [terraform_data.network, lxd_instance_file.frr]
  provisioner "local-exec" {
    command     = <<-SCRIPT
      lxc exec --project "$PROJECT" "$INSTANCE" -- sh -ceu '
        umask 077
        vtysh -C -f /root/network-lab/frr.conf > /root/network-lab/frr-check.log 2>&1
        /usr/lib/frr/frr-reload.py --test /root/network-lab/frr.conf > /root/network-lab/frr-test.log 2>&1
        install -o frr -g frr -m 0640 /root/network-lab/frr.conf /etc/frr/frr.conf
        /usr/lib/frr/frr-reload.py --reload /etc/frr/frr.conf > /root/network-lab/frr-reload.log 2>&1
      '
    SCRIPT
    environment = { PROJECT = var.project, INSTANCE = each.key }
  }
}
