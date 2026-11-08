terraform {
  required_version = ">= 1.7"
  required_providers {
    lxd = { source = "terraform-lxd/lxd", version = "3.1.1" }
  }
}
variable "workers" {
  type = map(object({ project = string, uuid = string, generation = string, networkd = string, lldp = string }))
}
data "lxd_instance" "worker" {
  for_each = var.workers
  name     = each.key
  project  = each.value.project
  lifecycle {
    postcondition {
      condition     = self.config["user.maas-bird-owner"] == "network-lab" && self.config["volatile.uuid"] == each.value.uuid
      error_message = "Worker ownership or hardware identity changed."
    }
  }
}
resource "lxd_instance_file" "networkd" {
  for_each           = var.workers
  instance           = data.lxd_instance.worker[each.key].name
  project            = each.value.project
  target_path        = "/etc/systemd/networkd.conf.d/90-network-lab.conf"
  content            = each.value.networkd
  mode               = "0644"
  uid                = 0
  gid                = 0
  create_directories = true
}
resource "terraform_data" "networkd" {
  for_each         = var.workers
  triggers_replace = [each.value.uuid, sha256(lxd_instance_file.networkd[each.key].content), each.value.generation]
  provisioner "local-exec" {
    command     = "lxc exec --project \"$PROJECT\" \"$INSTANCE\" -- systemctl restart systemd-networkd"
    environment = { PROJECT = each.value.project, INSTANCE = each.key }
  }
}
resource "lxd_instance_file" "lldp" {
  for_each    = var.workers
  instance    = data.lxd_instance.worker[each.key].name
  project     = each.value.project
  target_path = "/etc/default/lldpd"
  content     = each.value.lldp
  mode        = "0644"
  uid         = 0
  gid         = 0
}
resource "terraform_data" "lldp" {
  for_each         = var.workers
  triggers_replace = [each.value.uuid, sha256(lxd_instance_file.lldp[each.key].content), each.value.generation]
  provisioner "local-exec" {
    command     = "lxc exec --project \"$PROJECT\" \"$INSTANCE\" -- systemctl restart lldpd"
    environment = { PROJECT = each.value.project, INSTANCE = each.key }
  }
}
output "workers" {
  value      = keys(var.workers)
  depends_on = [terraform_data.networkd, terraform_data.lldp]
}
