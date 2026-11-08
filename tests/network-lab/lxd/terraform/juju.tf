variable "controller_ssh_public_key_path" {
  type        = string
  description = "Path relative to this module; only the public key enters state."
  default     = "../../runtime/juju-controller-key.pub"
}

locals {
  controller_mac = "02:00:00:ee:00:12"
}

resource "lxd_instance" "juju" {
  project  = lxd_project.lab.name
  name     = "juju"
  image    = "ubuntu:24.04"
  type     = "virtual-machine"
  profiles = []
  config = {
    "limits.cpu"           = "2"
    "limits.memory"        = "4GiB"
    "boot.autostart"       = "false"
    "user.maas-bird-owner" = "network-lab"
    "cloud-init.user-data" = "#cloud-config\n${yamlencode({
      ssh_pwauth          = false
      disable_root        = true
      ssh_authorized_keys = [trimspace(file("${path.module}/${var.controller_ssh_public_key_path}"))]
    })}"
    "cloud-init.network-config" = yamlencode({
      version = 2
      ethernets = {
        management = {
          match       = { macaddress = local.controller_mac }
          "set-name"  = "mgmt0"
          dhcp4       = false
          dhcp6       = false
          addresses   = ["10.203.240.12/24"]
          routes      = [{ to = "default", via = "10.203.240.1" }]
          nameservers = { addresses = ["10.203.240.1"] }
        }
      }
    })
  }
  device {
    name = "root"
    type = "disk"
    properties = {
      pool = "default"
      path = "/"
      size = "32GiB"
    }
  }
  device {
    name = "management"
    type = "nic"
    properties = {
      network = lxd_network.management.name
      hwaddr  = local.controller_mac
    }
  }
  wait_for {
    type = "agent"
  }
}
