data "lxd_instance" "switch" {
  for_each = local.nodes
  name     = lxd_instance.switch[each.key].name
  project  = var.project
}
resource "terraform_data" "ready" {
  for_each         = local.nodes
  depends_on       = [terraform_data.core_mtu]
  triggers_replace = data.lxd_instance.switch[each.key].config["volatile.uuid"]
  provisioner "local-exec" {
    command     = "lxc exec --project \"$PROJECT\" \"$INSTANCE\" -- timeout ${var.cloud_init_timeout_seconds} cloud-init status --wait"
    environment = { PROJECT = var.project, INSTANCE = each.key }
  }
}
resource "lxd_instance_file" "lldp" {
  for_each           = local.nodes
  instance           = each.key
  project            = var.project
  content            = contains(local.tors, each.key) ? "DAEMON_ARGS=\"-I fast0 -C fast0 -k\"\n" : "DAEMON_ARGS=\"-I r1a,r1b,r2a,r2b -C r1a -k\"\n"
  target_path        = "/root/network-lab/lldpd.default"
  mode               = "0600"
  uid                = 0
  gid                = 0
  create_directories = true
  depends_on         = [terraform_data.ready]
}
resource "terraform_data" "lldp" {
  for_each         = local.nodes
  triggers_replace = [sha256(lxd_instance_file.lldp[each.key].content), data.lxd_instance.switch[each.key].config["volatile.uuid"]]
  depends_on       = [lxd_instance_file.lldp]
  provisioner "local-exec" {
    command     = "lxc exec --project \"$PROJECT\" \"$INSTANCE\" -- sh -ceu 'install -m 0644 /root/network-lab/lldpd.default /etc/default/lldpd; systemctl restart lldpd'"
    environment = { PROJECT = var.project, INSTANCE = each.key }
  }
}
output "tor_chassis" {
  value      = { for name in local.tors : name => data.lxd_instance.switch[name].config["volatile.fast0.hwaddr"] }
  depends_on = [terraform_data.lldp]
}
output "switch_uuids" { value = { for name, instance in data.lxd_instance.switch : name => instance.config["volatile.uuid"] } }
