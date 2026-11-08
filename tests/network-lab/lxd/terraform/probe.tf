# A disk-empty machine for actual MAAS PXE/commission/deploy checks. It starts
# stopped: the operator runner must first transfer lab DHCP ownership to MAAS.
resource "lxd_network" "probe_fast" {
  for_each = toset(["0", "1"])
  name     = "nlab-probe-pf${each.key}"
  project  = "default"
  type     = "bridge"
  config = {
    "ipv4.address" = "none"
    "ipv6.address" = "none"
  }
}

resource "lxd_network" "rack2_fast" {
  for_each = toset(["0", "1"])
  name     = "nlab-r2-pf${each.key}"
  project  = "default"
  type     = "bridge"
  config = {
    "ipv4.address" = "none"
    "ipv6.address" = "none"
  }
}

resource "lxd_instance" "probe" {
  for_each = jsondecode(file("${path.module}/../../maas/network-model/hosts.json"))
  project  = lxd_project.lab.name
  name     = each.key
  type     = "virtual-machine"
  profiles = []
  running  = false
  config = {
    "limits.cpu"           = "2"
    "limits.memory"        = "4GiB"
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
      size            = "24GiB"
      "boot.priority" = "0"
    }
  }
  device {
    name = "mgmt0"
    type = "nic"
    properties = {
      network         = lxd_network.management.name
      "boot.priority" = "10"
    }
  }
  device {
    name = "mgmt1"
    type = "nic"
    properties = {
      network         = lxd_network.management.name
      "boot.priority" = "1"
    }
  }
  dynamic "device" {
    for_each = each.value.rack == 1 ? lxd_network.probe_fast : lxd_network.rack2_fast
    content {
      name = "pf${device.key}"
      type = "nic"
      properties = {
        network       = device.value.name
        "vlan.tagged" = "3400-3404"
      }
    }
  }
  # MAAS owns power state after enrollment. Terraform must not stop a deployed
  # machine during a routine refresh/apply.
  lifecycle {
    ignore_changes = [running]
  }
}

moved {
  from = lxd_instance.probe
  to   = lxd_instance.probe["probe"]
}
