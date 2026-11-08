terraform {
  required_version = ">= 1.6"
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
variable "cpu_cores" {
  type    = number
  default = 2
  validation {
    condition     = var.cpu_cores >= 1 && var.cpu_cores == floor(var.cpu_cores)
    error_message = "CPU allocation must be a positive whole number."
  }
}
variable "management_network" { type = string }
variable "management_cidr" { type = string }
variable "admin_password" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{32,}$", var.admin_password))
    error_message = "Use a random URL-safe password with at least 32 characters."
  }
}
variable "credential_directory" { type = string }
variable "image" {
  type    = string
  default = "a80e9c9fd0d484dec024da3e9e264bbad4ce159b5312d6e149ef6ad532e18e43"
}
locals {
  address = cidrhost(var.management_cidr, 10)
  gateway = cidrhost(var.management_cidr, 1)
}
resource "tls_private_key" "ca" {
  algorithm = "RSA"
  rsa_bits  = 3072
}
resource "tls_self_signed_cert" "ca" {
  private_key_pem       = tls_private_key.ca.private_key_pem
  is_ca_certificate     = true
  validity_period_hours = 720
  allowed_uses          = ["cert_signing", "crl_signing", "digital_signature"]
  subject { common_name = "MAAS network lab CA" }
}
resource "tls_private_key" "server" {
  algorithm = "RSA"
  rsa_bits  = 3072
}
resource "tls_cert_request" "server" {
  private_key_pem = tls_private_key.server.private_key_pem
  ip_addresses    = [local.address, "127.0.0.1"]
  subject { common_name = "maas.lab.invalid" }
}
resource "tls_locally_signed_cert" "server" {
  cert_request_pem      = tls_cert_request.server.cert_request_pem
  ca_private_key_pem    = tls_private_key.ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.ca.cert_pem
  validity_period_hours = 720
  allowed_uses          = ["server_auth", "digital_signature", "key_encipherment"]
}
locals {
  bootstrap = {
    "cloud-init.network-config" = yamlencode({
      version = 2
      ethernets = {
        mgmt0 = {
          match       = { macaddress = "02:00:00:fb:00:10" }, "set-name" = "mgmt0",
          dhcp4       = false, dhcp6 = false, "accept-ra" = false, "link-local" = [],
          addresses   = ["${local.address}/24"], routes = [{ to = "default", via = local.gateway }],
          nameservers = { addresses = ["1.1.1.1"] }
        }
      }
    })
    "cloud-init.user-data" = "#cloud-config\n${yamlencode({
      package_update = true
      packages       = ["jq", "curl", "openssl"]
      write_files = [
        { path = "/var/snap/maas/common/network-lab-tls/ca.crt", permissions = "0600", content = tls_self_signed_cert.ca.cert_pem },
        { path = "/var/snap/maas/common/network-lab-tls/server.crt", permissions = "0600", content = tls_locally_signed_cert.server.cert_pem },
        { path = "/var/snap/maas/common/network-lab-tls/server.key", permissions = "0600", content = tls_private_key.server.private_key_pem },
        { path = "/root/network-lab/admin-password", permissions = "0600", content = var.admin_password },
      ]
      runcmd = [
        ["snap", "install", "maas", "--channel=3.8/stable", "--revision=43385"],
        ["snap", "install", "maas-test-db", "--channel=3.7/stable", "--revision=185"],
        ["maas", "init", "region+rack", "--database-uri=maas-test-db:///", "--maas-url=http://${local.address}:5240/MAAS"],
        ["maas", "config-tls", "enable", "--yes", "--cacert", "/var/snap/maas/common/network-lab-tls/ca.crt", "/var/snap/maas/common/network-lab-tls/server.key", "/var/snap/maas/common/network-lab-tls/server.crt"],
        ["sh", "-ceu", "install -m 0644 /var/snap/maas/common/network-lab-tls/ca.crt /usr/local/share/ca-certificates/network-lab-maas.crt; update-ca-certificates; timeout 180 sh -c 'until curl --fail --silent https://127.0.0.1:5443/MAAS/api/2.0/version/ >/dev/null; do sleep 2; done'"],
        ["sh", "-ceu", "maas createadmin --username=lab --email=lab@example.invalid --password=\"$(cat /root/network-lab/admin-password)\"; umask 077; maas apikey --username=lab > /root/network-lab/api-key; maas login lab https://127.0.0.1:5443/MAAS \"$(cat /root/network-lab/api-key)\" >/dev/null"],
      ]
    })}"
  }
}
resource "terraform_data" "bootstrap" { triggers_replace = local.bootstrap }
resource "lxd_instance" "maas" {
  project  = var.project
  name     = "maas"
  type     = "virtual-machine"
  image    = var.image
  profiles = []
  config = merge({
    "limits.cpu"           = tostring(var.cpu_cores), "limits.memory" = "8GiB", "boot.autostart" = "false",
    "user.maas-bird-owner" = "network-lab"
  }, local.bootstrap)
  device {
    name       = "root"
    type       = "disk"
    properties = { pool = "default", path = "/", size = "32GiB" }
  }
  device {
    name       = "mgmt0"
    type       = "nic"
    properties = { network = var.management_network, hwaddr = "02:00:00:fb:00:10" }
  }
  lifecycle { replace_triggered_by = [terraform_data.bootstrap] }
}
resource "terraform_data" "credentials" {
  triggers_replace = terraform_data.bootstrap.id
  lifecycle { replace_triggered_by = [lxd_instance.maas] }
  provisioner "local-exec" {
    environment = { PROJECT = var.project, DIRECTORY = var.credential_directory }
    command     = <<-SCRIPT
      set -eu
      umask 077
      mkdir -p "$DIRECTORY"
      chmod 0700 "$DIRECTORY"
      timeout 300 sh -c 'until lxc exec --project "$PROJECT" maas -- true 2>/dev/null; do sleep 2; done'
      lxc exec --project "$PROJECT" maas -- timeout 900 cloud-init status --wait
      lxc file pull --project "$PROJECT" maas/var/snap/maas/common/network-lab-tls/ca.crt "$DIRECTORY/ca.crt"
      lxc file pull --project "$PROJECT" maas/root/network-lab/api-key "$DIRECTORY/api-key"
      chmod 0600 "$DIRECTORY/ca.crt" "$DIRECTORY/api-key"
    SCRIPT
  }
}
output "api_url" { value = "https://${local.address}:5443/MAAS" }
output "ca_certificate_path" { value = "${var.credential_directory}/ca.crt" }
output "api_key_path" { value = "${var.credential_directory}/api-key" }

output "cpu_cores" { value = tonumber(lxd_instance.maas.config["limits.cpu"]) }
