terraform {
  required_version = ">= 1.6"
  required_providers {
    maas = {
      source  = "canonical/maas"
      version = "2.10.0"
    }
  }
}

variable "api_key" {
  type      = string
  sensitive = true
}

variable "ca_certificate" {
  type = string
}

provider "maas" {
  api_url             = "https://10.203.240.10:5443/MAAS"
  api_key             = var.api_key
  tls_ca_cert_path    = var.ca_certificate
  installation_method = "snap"
}

resource "maas_fabric" "probe" {
  for_each = toset(["a", "b"])
  name     = "tf-provider-probe-${each.key}"
}

resource "maas_vlan" "probe" {
  for_each = maas_fabric.probe
  fabric   = each.value.id
  vid      = 3404
  name     = "general"
  mtu      = 1500
  dhcp_on  = false
}
