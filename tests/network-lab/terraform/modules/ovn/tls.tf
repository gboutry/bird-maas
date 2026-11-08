resource "tls_private_key" "ca" {
  algorithm = "RSA"
  rsa_bits  = 3072
}
resource "tls_self_signed_cert" "ca" {
  private_key_pem       = tls_private_key.ca.private_key_pem
  is_ca_certificate     = true
  validity_period_hours = 720
  allowed_uses          = ["cert_signing", "crl_signing", "digital_signature"]
  subject { common_name = "Network laboratory OVN CA" }
}
resource "tls_private_key" "server" {
  algorithm = "RSA"
  rsa_bits  = 3072
}
resource "tls_cert_request" "server" {
  private_key_pem = tls_private_key.server.private_key_pem
  ip_addresses    = [var.central_address]
  subject { common_name = "network-lab-ovn" }
}
resource "tls_locally_signed_cert" "server" {
  cert_request_pem      = tls_cert_request.server.cert_request_pem
  ca_private_key_pem    = tls_private_key.ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.ca.cert_pem
  validity_period_hours = 720
  allowed_uses          = ["server_auth", "digital_signature", "key_encipherment"]
}
resource "tls_private_key" "client" {
  for_each  = local.clients
  algorithm = "RSA"
  rsa_bits  = 3072
}
resource "tls_cert_request" "client" {
  for_each        = local.clients
  private_key_pem = tls_private_key.client[each.key].private_key_pem
  subject { common_name = each.value.uuid }
}
resource "tls_locally_signed_cert" "client" {
  for_each              = local.clients
  cert_request_pem      = tls_cert_request.client[each.key].cert_request_pem
  ca_private_key_pem    = tls_private_key.ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.ca.cert_pem
  validity_period_hours = 720
  allowed_uses          = ["client_auth", "digital_signature"]
}
resource "lxd_instance_file" "central_ca" {
  instance    = var.central_instance
  project     = var.project
  target_path = "${local.directory}/ca.crt"
  content     = tls_self_signed_cert.ca.cert_pem
  mode        = "0600"
  uid         = 0
  gid         = 0
  depends_on  = [terraform_data.central_preflight]
}
resource "lxd_instance_file" "central_cert" {
  instance    = var.central_instance
  project     = var.project
  target_path = "${local.directory}/server.crt"
  content     = tls_locally_signed_cert.server.cert_pem
  mode        = "0600"
  uid         = 0
  gid         = 0
  depends_on  = [terraform_data.central_preflight]
}
resource "lxd_instance_file" "central_key" {
  instance    = var.central_instance
  project     = var.project
  target_path = "${local.directory}/server.key"
  content     = tls_private_key.server.private_key_pem
  mode        = "0600"
  uid         = 0
  gid         = 0
  depends_on  = [terraform_data.central_preflight]
}
resource "lxd_instance_file" "central_owner" {
  instance    = var.central_instance
  project     = var.project
  target_path = "${local.directory}/terraform-owner"
  content     = "${var.project}\n"
  mode        = "0600"
  uid         = 0
  gid         = 0
  depends_on  = [terraform_data.central_preflight]
}
resource "lxd_instance_file" "client_ca" {
  for_each    = local.clients
  instance    = each.key
  project     = var.project
  target_path = "${local.directory}/ca.crt"
  content     = tls_self_signed_cert.ca.cert_pem
  mode        = "0600"
  uid         = 0
  gid         = 0
  depends_on  = [terraform_data.client_preflight]
}
resource "lxd_instance_file" "client_cert" {
  for_each    = local.clients
  instance    = each.key
  project     = var.project
  target_path = "${local.directory}/client.crt"
  content     = tls_locally_signed_cert.client[each.key].cert_pem
  mode        = "0600"
  uid         = 0
  gid         = 0
  depends_on  = [terraform_data.client_preflight]
}
resource "lxd_instance_file" "client_key" {
  for_each    = local.clients
  instance    = each.key
  project     = var.project
  target_path = "${local.directory}/client.key"
  content     = tls_private_key.client[each.key].private_key_pem
  mode        = "0600"
  uid         = 0
  gid         = 0
  depends_on  = [terraform_data.client_preflight]
}
resource "lxd_instance_file" "client_owner" {
  for_each    = local.clients
  instance    = each.key
  project     = var.project
  target_path = "${local.directory}/terraform-owner"
  content     = "${var.project}\n"
  mode        = "0600"
  uid         = 0
  gid         = 0
  depends_on  = [terraform_data.client_preflight]
}
resource "lxd_instance_file" "system_id" {
  for_each    = local.active
  instance    = each.key
  project     = var.project
  target_path = "/etc/openvswitch/system-id.conf"
  content     = "${each.value.uuid}\n"
  mode        = "0644"
  uid         = 0
  gid         = 0
  depends_on  = [terraform_data.client_preflight]
}
resource "lxd_instance_file" "endpoint" {
  for_each    = local.compute
  instance    = each.key
  project     = var.project
  target_path = "/etc/systemd/network/04-network-lab-tenant.network"
  content     = <<-NETWORK
    [Match]
    Name=tenant0
    [Link]
    MTUBytes=${var.tenant_mtu}
    [Network]
    Address=${each.value.endpoint.address}/24
    LinkLocalAddressing=no
    IPv6AcceptRA=no
    [Route]
    Destination=172.30.10.0/24
    Scope=link
    Table=4500
    [Route]
    Destination=0.0.0.0/0
    Gateway=172.30.10.1
    GatewayOnLink=yes
    Table=4500
    [Route]
    Destination=0.0.0.0/0
    Type=unreachable
    Metric=42760
    Table=4500
    [RoutingPolicyRule]
    From=${each.value.endpoint.address}/32
    Table=4500
    Priority=15000
  NETWORK
  mode        = "0644"
  uid         = 0
  gid         = 0
  depends_on  = [terraform_data.client_preflight]
}
