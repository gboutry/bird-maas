locals {
  gateway_router = templatefile("${path.module}/router.sh.tftpl", {
    gateways  = var.gateways
    endpoints = { for name, client in local.clients : name => merge(client.endpoint, { floating = "192.0.2.${100 + index(local.hosts, name)}" }) if !client.gateway }
  })
}
resource "terraform_data" "router" {
  triggers_replace = sha256(local.gateway_router)
  depends_on       = [terraform_data.central, terraform_data.chassis]
  provisioner "local-exec" {
    command     = "lxc exec --project \"$PROJECT\" \"$INSTANCE\" -- sh -ceu \"$CONFIGURATION\""
    environment = { PROJECT = var.project, INSTANCE = var.central_instance, CONFIGURATION = local.gateway_router }
  }
}
output "router" { value = "nlab-tf-router" }
