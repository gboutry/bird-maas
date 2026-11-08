locals {
  # Noble networkd preserves foreign routes but removes foreign nexthop objects.
  # Inline IPv6 nexthops keep zebra's routes intact across networkd restarts.
  routing = { for name, gateway in local.gateways : name => "no zebra nexthop kernel enable\n${templatefile("${path.module}/frr.conf.tftpl", {
    gateway = gateway, password = var.passwords[name]
  })}" }
}

resource "lxd_instance_file" "routing" {
  for_each           = local.gateways
  instance           = data.lxd_instance.gateway[each.key].name
  project            = var.project
  target_path        = "/root/network-lab/frr.conf"
  content            = local.routing[each.key]
  mode               = "0600"
  uid                = 0
  gid                = 0
  create_directories = true
  depends_on         = [terraform_data.ready]
}

resource "terraform_data" "routing" {
  for_each         = local.gateways
  triggers_replace = [data.lxd_instance.gateway[each.key].config["volatile.uuid"], sha256(local.routing[each.key])]
  depends_on       = [lxd_instance_file.routing]
  provisioner "local-exec" {
    command     = <<-SCRIPT
      lxc exec --project "$PROJECT" "$INSTANCE" -- sh -ceu '
        umask 077
        vtysh -C -f /root/network-lab/frr.conf > /root/network-lab/frr-check.log 2>&1
        install -o frr -g frr -m 0640 /root/network-lab/frr.conf /etc/frr/frr.conf
        systemctl restart frr
      '
    SCRIPT
    environment = { PROJECT = var.project, INSTANCE = each.value.name }
  }
}
