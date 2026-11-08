resource "lxd_instance_file" "lldp" {
  for_each    = local.gateways
  instance    = data.lxd_instance.gateway[each.key].name
  project     = var.project
  target_path = "/etc/default/lldpd"
  content     = "DAEMON_ARGS=\"-r -I fast0 -k\"\n"
  mode        = "0644"
  uid         = 0
  gid         = 0
  depends_on  = [terraform_data.ready]
}
resource "terraform_data" "lldp" {
  for_each         = local.gateways
  triggers_replace = [data.lxd_instance.gateway[each.key].config["volatile.uuid"], sha256(lxd_instance_file.lldp[each.key].content)]
  provisioner "local-exec" {
    command     = "lxc exec --project \"$PROJECT\" \"$INSTANCE\" -- systemctl restart lldpd"
    environment = { PROJECT = var.project, INSTANCE = each.value.name }
  }
}
