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

resource "lxd_project" "lab" {
  name        = "maas-bird-network-lab"
  description = "Disposable MAAS/BIRD validation; no production resources"
  config = {
    "features.images"          = "false"
    "features.profiles"        = "true"
    "features.networks"        = "false"
    "features.storage.volumes" = "true"
    "limits.cpu"               = "16"
    "limits.memory"            = "56GiB"
  }
}

# Private lab bridge only. No physical interface is attached. The MAAS VM uses
# static networking; MAAS exclusively owns DHCP for the PXE lifecycle probes.
resource "lxd_network" "management" {
  # LXD bridge networks live in default; only OVN supports project networks.
  project = "default"
  name    = "nlab-mgmt"
  type    = "bridge"
  config = {
    "ipv4.address" = "10.203.240.1/24"
    "ipv4.nat"     = "true"
    "ipv4.dhcp"    = "false"
    "ipv6.address" = "none"
    # Two management NICs of one VM share this L2. MAAS owns DNS identity;
    # LXD's per-NIC registration would reject the repeated instance name.
    "dns.mode" = "none"
  }
}

resource "lxd_instance" "maas" {
  project  = lxd_project.lab.name
  name     = "maas"
  image    = "ubuntu:24.04"
  type     = "virtual-machine"
  profiles = []
  config = {
    "limits.cpu"           = "4"
    "limits.memory"        = "8GiB"
    "boot.autostart"       = "false"
    "user.maas-bird-owner" = "network-lab"
    "cloud-init.user-data" = file("${path.module}/../../maas/bootstrap/cloud-init.yaml")
    "cloud-init.network-config" = yamlencode(
      yamldecode(file("${path.module}/../../maas/bootstrap/network.yaml")).network
    )
  }
  device {
    name = "root"
    type = "disk"
    properties = {
      pool = "default"
      path = "/"
      size = "48GiB"
    }
  }
  device {
    name = "eth0"
    type = "nic"
    properties = {
      network = lxd_network.management.name
    }
  }
  wait_for {
    type = "agent"
  }
}
