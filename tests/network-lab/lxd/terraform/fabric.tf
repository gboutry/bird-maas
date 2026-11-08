locals {
  fabric_mac = "02:00:00:ee:00:11"
  fabric_ports = {
    r1a = { mac = "02:00:00:ee:01:01", network = lxd_network.probe_fast["0"].name }
    r1b = { mac = "02:00:00:ee:01:02", network = lxd_network.probe_fast["1"].name }
    r2a = { mac = "02:00:00:ee:02:01", network = lxd_network.rack2_fast["0"].name }
    r2b = { mac = "02:00:00:ee:02:02", network = lxd_network.rack2_fast["1"].name }
  }
  fabric_bootstrap = {
    package_update = true
    packages       = ["ca-certificates", "docker.io", "iproute2", "iputils-ping", "tcpdump", "jq"]
    write_files = concat([
      {
        path        = "/etc/network-lab/versions.json"
        permissions = "0644"
        content     = file("${path.module}/../../containerlab/bootstrap/versions.json")
      },
      {
        path        = "/usr/local/sbin/install-containerlab.py"
        permissions = "0700"
        content     = file("${path.module}/../../containerlab/bootstrap/install_containerlab.py")
      },
      {
        path        = "/etc/systemd/system/network-lab-fabric.service"
        permissions = "0644"
        content     = file("${path.module}/../../containerlab/bootstrap/network-lab-fabric.service")
      },
      ], [for name, port in local.fabric_ports : {
        path        = "/etc/systemd/network/10-netplan-br-${name}.netdev.d/20-lldp.conf"
        permissions = "0644"
        content     = "[Bridge]\nGroupForwardMask=16384\n"
    }])
    runcmd = [
      ["python3", "/usr/local/sbin/install-containerlab.py"],
      ["systemctl", "enable", "--now", "docker"],
      ["systemctl", "enable", "network-lab-fabric.service"],
    ]
  }
}

resource "lxd_instance" "fabric" {
  project  = lxd_project.lab.name
  name     = "fabric"
  image    = "ubuntu:24.04"
  type     = "virtual-machine"
  profiles = []
  config = {
    "limits.cpu"           = "2"
    "limits.memory"        = "8GiB"
    "boot.autostart"       = "false"
    "user.maas-bird-owner" = "network-lab"
    "cloud-init.user-data" = "#cloud-config\n${yamlencode(local.fabric_bootstrap)}"
    "cloud-init.network-config" = yamlencode({
      version = 2
      ethernets = merge({
        management = {
          match       = { macaddress = local.fabric_mac }
          "set-name"  = "mgmt0"
          dhcp4       = false
          dhcp6       = false
          addresses   = ["10.203.240.11/24"]
          routes      = [{ to = "default", via = "10.203.240.1" }]
          nameservers = { addresses = ["10.203.240.1"] }
        }
        }, { for name, port in local.fabric_ports : "wire-${name}" => {
          match        = { macaddress = port.mac }
          "set-name"   = "wire-${name}"
          dhcp4        = false
          dhcp6        = false
          "accept-ra"  = false
          "link-local" = []
          optional     = true
      } })
      bridges = { for name, port in local.fabric_ports : "br-${name}" => {
        interfaces   = ["wire-${name}"]
        dhcp4        = false
        dhcp6        = false
        "accept-ra"  = false
        "link-local" = []
        optional     = true
        parameters   = { stp = false }
      } }
    })
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
    name = "management"
    type = "nic"
    properties = {
      network = lxd_network.management.name
      hwaddr  = local.fabric_mac
    }
  }
  dynamic "device" {
    for_each = local.fabric_ports
    content {
      name = "wire-${device.key}"
      type = "nic"
      properties = {
        network       = device.value.network
        hwaddr        = device.value.mac
        "vlan.tagged" = "3400-3404"
      }
    }
  }
  wait_for {
    type = "agent"
  }
}
