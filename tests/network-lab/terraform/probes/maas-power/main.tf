terraform {
  required_version = ">= 1.6"
  required_providers {
    lxd = {
      source  = "terraform-lxd/lxd"
      version = "3.1.1"
    }
    maas = {
      source  = "canonical/maas"
      version = "2.10.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "4.4.2"
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

variable "enroll" {
  type    = bool
  default = false
}

provider "lxd" {
  remote {
    name    = "local"
    address = "unix://"
  }
}

provider "maas" {
  api_url             = "https://10.203.240.10:5443/MAAS"
  api_key             = var.api_key
  tls_ca_cert_path    = var.ca_certificate
  installation_method = "snap"
}

resource "lxd_project" "probe" {
  name = "maas-bird-tf-probe"
  config = {
    "features.images"          = "false"
    "features.profiles"        = "true"
    "features.networks"        = "false"
    "features.storage.volumes" = "true"
    "limits.cpu"               = "1"
    "limits.memory"            = "3GiB"
    "user.maas-bird-owner"     = "network-lab"
  }
}

resource "tls_private_key" "power" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "tls_self_signed_cert" "power" {
  private_key_pem       = tls_private_key.power.private_key_pem
  validity_period_hours = 24
  allowed_uses          = ["client_auth", "digital_signature"]
  subject {
    common_name = "maas-bird-tf-probe-power"
  }
}

resource "lxd_trust_certificate" "power" {
  name     = "maas-bird-tf-probe-power"
  content  = tls_self_signed_cert.power.cert_pem
  projects = [lxd_project.probe.name]
  type     = "client"
}

resource "lxd_instance" "probe" {
  name     = "tf-power-probe"
  project  = lxd_project.probe.name
  type     = "virtual-machine"
  running  = false
  profiles = []
  config = {
    "limits.cpu"           = "1"
    "limits.memory"        = "2GiB"
    "boot.mode"            = "uefi-nosecureboot"
    "boot.autostart"       = "false"
    "user.maas-bird-owner" = "network-lab"
  }
  device {
    name = "root"
    type = "disk"
    properties = {
      pool            = "default"
      path            = "/"
      size            = "8GiB"
      "boot.priority" = "0"
    }
  }
  device {
    name = "mgmt0"
    type = "nic"
    properties = {
      network         = "nlab-mgmt"
      hwaddr          = "02:00:00:fa:fd:01"
      "boot.priority" = "10"
    }
  }
  lifecycle {
    ignore_changes = [running]
  }
}

resource "maas_machine" "probe" {
  count           = var.enroll ? 1 : 0
  hostname        = lxd_instance.probe.name
  architecture    = "amd64/generic"
  min_hwe_kernel  = "hwe-24.04"
  testing_scripts = ["none"]
  pxe_mac_address = "02:00:00:fa:fd:01"
  power_type      = "lxd"
  power_parameters = jsonencode({
    power_address = "https://10.203.240.10:944"
    project       = lxd_project.probe.name
    instance_name = lxd_instance.probe.name
    certificate   = tls_self_signed_cert.power.cert_pem
    key           = tls_private_key.power.private_key_pem
  })
  depends_on = [lxd_trust_certificate.power, terraform_data.proxy_ready]
  lifecycle {
    precondition {
      condition     = var.proxy_enabled
      error_message = "Power control requires the verified TLS gateway; direct MAAS LXD TLS verification is disabled upstream."
    }
  }
  timeouts {
    create = "20m"
  }
}
