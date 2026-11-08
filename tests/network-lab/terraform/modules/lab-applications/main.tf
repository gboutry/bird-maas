terraform {
  required_version = ">= 1.7"
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
variable "worker_configuration" {
  type    = map(object({ project = string, uuid = string, generation = string, networkd = string, lldp = string }))
  default = {}
}
variable "workers" { type = any }
variable "machines" { type = map(string) }
variable "model_uuid" { type = string }
variable "client_directory" { type = string }
variable "output_directory" { type = string }
variable "charm_directory" { type = string }
variable "roles_path" { type = string }
variable "tor_chassis" { type = map(string) }
variable "maas_secret_uri" { type = string }
variable "maas_ca_secret_uri" { type = string }
variable "bgp_secret_uris" { type = map(string) }
variable "gateway_prefixes" {
  type    = list(string)
  default = []
}
variable "enabled" {
  type    = bool
  default = false
}
locals {
  hosts        = ["probe", "probe-alt", "probe-r2a", "probe-r2b"]
  roles        = jsondecode(file(var.roles_path)).roles
  applications = { for name, worker in var.workers : name => "r${worker.rack}${index(local.hosts, name) % 2 == 0 ? "a" : "b"}" }
  profiles = [for rack in [1, 2] : {
    id = "r${rack}"
    peers = [for side in ["a", "b"] : {
      id  = "r${rack}${side}", chassis_id = var.tor_chassis["r${rack}${side}"],
      asn = 65101 + (rack - 1) * 2 + (side == "a" ? 0 : 1)
    }]
  }]
  attachments = { for name, worker in var.workers : name => flatten([for role in local.roles : [for pf in [0, 1] : {
    identity        = { scheme = "permanent-mac", value = worker.interfaces["pf${pf}"].mac }
    function        = { controller = 0, pf = 0, vf = null }
    id              = "${name}-${role.id == "general" ? "gp" : role.id}-r${worker.rack}${pf == 0 ? "a" : "b"}"
    domain          = role.domain, vlan = role.vlan
    chassis_id      = var.tor_chassis["r${worker.rack}${pf == 0 ? "a" : "b"}"]
    peer_link_local = "fe80::1", uplink = worker.interfaces["pf${pf}"].name
  }]]) }
  domains = { for name, worker in var.workers : name => [for domain in ["general", "ceph-access", "ceph-replication"] : {
    id               = domain, kernel_ecmp_limit = 2
    import_prefixes  = concat(flatten([for role in local.roles : [for other in local.hosts : "${cidrhost(role.address_block, 20 + index(local.hosts, other))}/32" if domain == "general" || other != name] if role.domain == domain]), domain == "general" ? var.gateway_prefixes : [])
    export_allowlist = [for role in local.roles : "${cidrhost(role.address_block, 20 + index(local.hosts, name))}/32" if role.domain == domain]
    contexts = [for role in local.roles : {
      id             = role.context, machine_id = worker.system_id, numa_node = null
      attachment_ids = [for side in ["a", "b"] : "${name}-${role.id == "general" ? "gp" : role.id}-r${worker.rack}${side}"]
      table          = role.table, rule_priority = role.rule_priority
      sources        = ["${cidrhost(role.address_block, 20 + index(local.hosts, name))}/32"]
    } if role.domain == domain]
  }] }
}
module "applications" {
  source           = "../juju-local-applications"
  model            = "nlab-tf:network-lab"
  model_uuid       = var.model_uuid
  client_directory = var.client_directory
  output_directory = var.output_directory
  charms           = { for name in ["maas-inventory", "dpu-attachments", "bird"] : name => "${var.charm_directory}/${name}_amd64.charm" }
  applications = merge({
    inventory = {
      charm = "maas-inventory", machine = var.machines["probe"], constraints = "arch=amd64"
      options = {
        maas-url            = "https://10.203.241.10:5443/MAAS/", maas-credentials = var.maas_secret_uri,
        maas-ca-certificate = var.maas_ca_secret_uri, rack-profiles = jsonencode(local.profiles)
        host-identities = jsonencode([for name, worker in var.workers : {
          machine_id = worker.system_id, local_asn = 4200000020 + index(local.hosts, name),
          router_id  = "10.205.10.${20 + index(local.hosts, name)}"
        }])
      }
    }
    }, { for name, worker in var.workers : "nic-${local.applications[name]}" => {
      charm   = "dpu-attachments", machine = var.machines[name], constraints = "arch=amd64"
      options = { backend = "native-nic", attachments = jsonencode(local.attachments[name]) }
      } }, { for name, worker in var.workers : "bird-${local.applications[name]}" => {
      charm = "bird", machine = var.machines[name], constraints = "arch=amd64"
      options = {
        enabled            = tostring(var.enabled), advertisement-mode = "node-addresses", policy-protocol = "4"
        protected-prefixes = "10.203.0.0/16", bgp-credentials = var.bgp_secret_uris[name]
        routing-domains    = jsonencode(local.domains[name])
      }
  } })
}
module "worker_config" {
  source     = "../worker-config"
  workers    = var.worker_configuration
  depends_on = [module.applications]
}
output "worker_applications" {
  value      = local.applications
  depends_on = [module.worker_config]
}

output "enabled" {
  value      = var.enabled
  depends_on = [module.worker_config]
}
output "deployment_generation" {
  value      = module.applications.deployment_generation
  depends_on = [module.worker_config]
}
