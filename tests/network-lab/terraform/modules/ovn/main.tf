terraform {
  required_providers {
    lxd = { source = "terraform-lxd/lxd", version = "3.1.1" }
    tls = { source = "hashicorp/tls", version = "4.4.2" }
  }
}
provider "lxd" {
  remote {
    name    = "local"
    address = "unix://"
  }
}
variable "project" { type = string }
variable "tenant_mtu" {
  type    = number
  default = 1400
  validation {
    condition     = var.tenant_mtu >= 1280 && var.tenant_mtu <= 1500
    error_message = "Use a tenant MTU between 1280 and 1500 bytes."
  }
}
variable "workers" { type = any }
variable "worker_generations" {
  description = "Optional model/machine deployment identities for same-UUID worker reprovisioning."
  type        = map(string)
  default     = {}
  validation {
    condition     = alltrue([for generation in values(var.worker_generations) : try(trimspace(generation) != "", false)])
    error_message = "Worker deployment generations must be nonempty strings."
  }
}
variable "gateways" { type = any }
variable "central_address" { type = string }
variable "central_cpu_cores" {
  type    = number
  default = 2
  validation {
    condition     = var.central_cpu_cores >= 1 && var.central_cpu_cores == floor(var.central_cpu_cores)
    error_message = "The central CPU allocation must be a positive whole number."
  }
}
variable "central_instance" {
  type    = string
  default = "maas"
}
variable "enable_compute" {
  type    = bool
  default = false
}
locals {
  directory = "/etc/ovn/network-lab"
  hosts     = ["probe", "probe-alt", "probe-r2a", "probe-r2b"]
  clients = merge(
    { for name, worker in var.workers : name => {
      uuid     = worker.system_uuid, gateway = false, vtep = "10.205.10.${20 + index(local.hosts, name)}"
      endpoint = { port = "tenant-${name}", address = "172.30.10.${20 + index(local.hosts, name)}", mac = format("02:00:00:ab:00:%02x", 20 + index(local.hosts, name)) }
    } },
    { for name, gateway in var.gateways : gateway.name => {
      uuid = gateway.system_uuid, gateway = true, vtep = gateway.address, endpoint = null
    } },
  )
  active  = { for name, client in local.clients : name => client if client.gateway || var.enable_compute }
  compute = { for name, client in local.active : name => client if !client.gateway }
}
data "lxd_instance" "central" {
  name    = var.central_instance
  project = var.project
  lifecycle {
    postcondition {
      condition     = self.config["user.maas-bird-owner"] == "network-lab" && self.config["limits.cpu"] == tostring(var.central_cpu_cores) && self.config["limits.memory"] == "8GiB"
      error_message = "OVN central requires the owned capped infrastructure VM."
    }
  }
}
data "lxd_instance" "client" {
  for_each = local.clients
  name     = each.key
  project  = var.project
  lifecycle {
    postcondition {
      condition     = self.config["user.maas-bird-owner"] == "network-lab" && self.config["volatile.uuid"] == each.value.uuid
      error_message = "Chassis ownership or native hardware identity changed."
    }
  }
}
resource "terraform_data" "central_preflight" {
  triggers_replace = data.lxd_instance.central.config["volatile.uuid"]
  lifecycle {
    precondition {
      condition     = length(setsubtract(toset(keys(var.worker_generations)), toset(keys(var.workers)))) == 0
      error_message = "Worker deployment generations may identify declared workers only."
    }
  }
  provisioner "local-exec" {
    command     = <<-SCRIPT
      lxc exec --project "$PROJECT" "$INSTANCE" -- sh -ceu '
        if test -e /etc/ovn/network-lab/terraform-owner; then
          test "$(cat /etc/ovn/network-lab/terraform-owner)" = "$1"
        else
          test ! -e /var/lib/ovn/ovnsb_db.db
          test ! -e /var/lib/ovn/ovnnb_db.db
        fi
        install -d -m 0700 /etc/ovn/network-lab
      ' network-lab "$PROJECT"
    SCRIPT
    environment = { PROJECT = var.project, INSTANCE = var.central_instance }
  }
}
resource "terraform_data" "client_preflight" {
  for_each         = local.clients
  triggers_replace = try("${each.value.uuid}/${var.worker_generations[each.key]}", each.value.uuid)
  provisioner "local-exec" {
    command     = <<-SCRIPT
      lxc exec --project "$PROJECT" "$INSTANCE" -- sh -ceu '
        if test -e /etc/ovn/network-lab/terraform-owner; then
          test "$(cat /etc/ovn/network-lab/terraform-owner)" = "$1"
        else
          ovs-vsctl --format=json --columns=external_ids list Open_vSwitch | jq -e ".data[0][0][1] | map({key:.[0],value:.[1]}) | from_entries | (.\"ovn-remote\" // \"\") == \"\"" >/dev/null
          test -z "$(ovs-vsctl get-ssl)"
        fi
        ip -j -4 address show dev lo | jq -e --arg ip "$2" "[.[].addr_info[] | select(.local==\$ip and .prefixlen==32)] | length==1" >/dev/null
        install -d -m 0700 /etc/ovn/network-lab
      ' network-lab "$PROJECT" "$VTEP"
    SCRIPT
    environment = { PROJECT = var.project, INSTANCE = each.key, VTEP = each.value.vtep }
  }
}
resource "terraform_data" "package" {
  triggers_replace = data.lxd_instance.central.config["volatile.uuid"]
  depends_on       = [lxd_instance_file.central_owner, terraform_data.ppa]
  provisioner "local-exec" {
    command     = "lxc exec --project \"$PROJECT\" \"$INSTANCE\" -- sh -ceu 'export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get -o DPkg::Lock::Timeout=600 install -y --no-install-recommends ovn-central'"
    environment = { PROJECT = var.project, INSTANCE = var.central_instance }
  }
}
resource "terraform_data" "central" {
  triggers_replace = [data.lxd_instance.central.config["volatile.uuid"], sha256(tls_locally_signed_cert.server.cert_pem)]
  depends_on       = [terraform_data.package, lxd_instance_file.central_ca, lxd_instance_file.central_cert, lxd_instance_file.central_key, lxd_instance_file.central_owner]
  provisioner "local-exec" {
    command     = <<-SCRIPT
      lxc exec --project "$PROJECT" "$INSTANCE" -- sh -ceu '
        ovn-nbctl --timeout=30 show >/dev/null
        ovn-sbctl --timeout=30 show >/dev/null
        ovn-sbctl set-ssl /etc/ovn/network-lab/server.key /etc/ovn/network-lab/server.crt /etc/ovn/network-lab/ca.crt
        ovn-sbctl set-connection role=ovn-controller "pssl:6642:$1"
        ovn-nbctl del-connection
        ovn-nbctl --may-exist ls-add nlab-tf-tenant
      ' network-lab "$ADDRESS"
    SCRIPT
    environment = { PROJECT = var.project, INSTANCE = var.central_instance, ADDRESS = var.central_address }
  }
}
resource "terraform_data" "logical_port" {
  for_each         = { for name, client in local.clients : name => client if !client.gateway }
  triggers_replace = [each.value.uuid, sha256(jsonencode(each.value.endpoint))]
  depends_on       = [terraform_data.central]
  provisioner "local-exec" {
    command     = <<-SCRIPT
      lxc exec --project "$PROJECT" "$INSTANCE" -- ovn-nbctl --may-exist lsp-add nlab-tf-tenant "$PORT" -- lsp-set-addresses "$PORT" "$MAC $ADDRESS" -- lsp-set-port-security "$PORT" "$MAC $ADDRESS" -- lsp-set-options "$PORT" "requested-chassis=$UUID"
    SCRIPT
    environment = { PROJECT = var.project, INSTANCE = var.central_instance, PORT = each.value.endpoint.port, MAC = each.value.endpoint.mac, ADDRESS = each.value.endpoint.address, UUID = each.value.uuid }
  }
}
resource "terraform_data" "chassis" {
  for_each         = local.active
  triggers_replace = concat([each.value.uuid, sha256(tls_locally_signed_cert.client[each.key].cert_pem), sha256(jsonencode(each.value)), var.central_address, each.value.gateway ? 0 : var.tenant_mtu], try([var.worker_generations[each.key]], []))
  depends_on       = [terraform_data.central, lxd_instance_file.client_ca, lxd_instance_file.client_cert, lxd_instance_file.client_key, lxd_instance_file.client_owner, lxd_instance_file.system_id, lxd_instance_file.endpoint, terraform_data.logical_port, terraform_data.ppa]
  provisioner "local-exec" {
    command = <<-SCRIPT
      set -eu
      if test "$GATEWAY" = false; then
        deadline=$(($(date +%s) + 180))
        until lxc exec --project "$PROJECT" "$INSTANCE" -- ip -j route show table 4400 "$REMOTE/32" | jq -e --argjson expected "$UPLINKS" 'length == 1 and .[0].protocol == "bird" and (.[0].nexthops | length == 2 and (map(.dev)|sort) == ($expected|sort) and all(.[]; .via.family == "inet6"))' >/dev/null; do
          test "$(date +%s)" -lt "$deadline"
          sleep 2
        done
      fi
      lxc exec --project "$PROJECT" "$INSTANCE" -- sh -ceu '
        ovs-vsctl set-ssl /etc/ovn/network-lab/client.key /etc/ovn/network-lab/client.crt /etc/ovn/network-lab/ca.crt
        ovs-vsctl set Open_vSwitch . "external_ids:system-id=$1" "external_ids:hostname=$2" "external_ids:ovn-remote=ssl:$3:6642" external_ids:ovn-encap-type=geneve "external_ids:ovn-encap-ip=$4" external_ids:ovn-set-local-ip=true
        if test "$5" = true; then
          ovs-vsctl set Open_vSwitch . external_ids:ovn-cms-options=enable-chassis-as-gw external_ids:ovn-bridge-mappings=physnet-provider:br-provider
        else
          ovs-vsctl remove Open_vSwitch . external_ids ovn-cms-options ovn-bridge-mappings
          ovs-vsctl --may-exist add-br br-int -- --may-exist add-port br-int tenant0 -- set Interface tenant0 type=internal "external_ids:iface-id=$6" "mac=\"$7\"" "mtu_request=$8"
          networkctl reload
          networkctl reconfigure tenant0
        fi
        systemctl restart ovn-controller
      ' network-lab "$UUID" "$INSTANCE" "$ADDRESS" "$VTEP" "$GATEWAY" "$PORT" "$MAC" "$MTU"
    SCRIPT
    environment = {
      PROJECT = var.project, INSTANCE = each.key, UUID = each.value.uuid, ADDRESS = var.central_address, VTEP = each.value.vtep, GATEWAY = tostring(each.value.gateway)
      PORT    = each.value.gateway ? "" : each.value.endpoint.port
      MAC     = each.value.gateway ? "" : each.value.endpoint.mac
      REMOTE  = each.value.gateway ? "" : "10.205.10.${var.workers[each.key].rack == 1 ? 22 : 20}"
      MTU     = tostring(var.tenant_mtu)
      UPLINKS = each.value.gateway ? "[]" : jsonencode([for vlan in var.workers[each.key].vlans : vlan.name if vlan.role == "general"])
    }
  }
}
output "central_instance" { value = var.central_instance }
output "central_address" { value = var.central_address }
output "chassis" { value = local.active }
output "compute_enabled" { value = var.enable_compute }
