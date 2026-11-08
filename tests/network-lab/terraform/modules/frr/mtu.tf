resource "terraform_data" "core_mtu" {
  for_each         = local.nodes
  triggers_replace = [var.core_mtu, data.lxd_instance.switch[each.key].config["volatile.uuid"]]
  provisioner "local-exec" {
    command = <<-SCRIPT
      set -eu
      restart=false
      for interface in $INTERFACES; do
        link=$(lxc config get --project "$PROJECT" "$INSTANCE" "volatile.$interface.host_name")
        test -n "$link"
        test "$(cat "/sys/class/net/$link/mtu")" -eq "$MTU" || restart=true
      done
      if test "$restart" = true; then
        lxc restart --project "$PROJECT" "$INSTANCE"
        lxc exec --project "$PROJECT" "$INSTANCE" -- timeout ${var.cloud_init_timeout_seconds} cloud-init status --wait
      fi
    SCRIPT
    environment = {
      PROJECT    = var.project, INSTANCE = each.key,
      INTERFACES = join(" ", keys(each.value.links)), MTU = tostring(var.core_mtu)
    }
  }
}
