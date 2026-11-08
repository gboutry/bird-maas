variable "proxy_enabled" {
  type    = bool
  default = false
}

variable "lxd_server_certificate" {
  type    = string
  default = ""
}

variable "lxd_server_name" {
  type    = string
  default = "localhost"
  validation {
    condition     = can(regex("^[A-Za-z0-9.-]+$", var.lxd_server_name))
    error_message = "The pinned TLS name must be a DNS name."
  }
}

data "lxd_instance" "maas" {
  name    = "maas"
  project = "maas-bird-network-lab"
  lifecycle {
    postcondition {
      condition = (
        self.type == "virtual-machine" &&
        self.config["user.maas-bird-owner"] == "network-lab" &&
        self.config["limits.cpu"] == "4" && self.config["limits.memory"] == "8GiB"
      )
      error_message = "TLS setup requires the owned, capped MAAS VM."
    }
  }
}

resource "tls_private_key" "proxy" {
  count     = var.proxy_enabled ? 1 : 0
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "tls_self_signed_cert" "proxy" {
  count                 = var.proxy_enabled ? 1 : 0
  private_key_pem       = tls_private_key.proxy[0].private_key_pem
  validity_period_hours = 24
  allowed_uses          = ["server_auth", "digital_signature", "key_encipherment"]
  ip_addresses          = ["127.0.0.1", "10.203.240.10"]
  dns_names             = ["localhost"]
  subject {
    common_name = "localhost"
  }
}

resource "terraform_data" "proxy_package" {
  count = var.proxy_enabled ? 1 : 0
  input = {
    instance = data.lxd_instance.maas.name
    project  = data.lxd_instance.maas.project
  }
  provisioner "local-exec" {
    command = <<-EOF
      lxc exec --project "$PROJECT" "$INSTANCE" -- sh -ceu '
        marker=/var/lib/network-lab/lxd-power-proxy-owned
        if test -e /etc/nginx/nginx.conf && ! test -e "$marker"; then
          echo "Refusing an existing unowned nginx installation" >&2
          exit 1
        fi
        install -d -m 0700 /var/lib/network-lab
        touch "$marker"
        systemctl mask nginx.service
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y nginx
        rm -f /etc/nginx/sites-enabled/default
      '
    EOF
    environment = {
      PROJECT  = self.input.project
      INSTANCE = self.input.instance
    }
  }
  provisioner "local-exec" {
    when    = destroy
    command = "lxc exec --project \"$PROJECT\" \"$INSTANCE\" -- sh -ceu 'systemctl disable --now nginx.service; rm -f /var/lib/network-lab/lxd-power-proxy-owned'"
    environment = {
      PROJECT  = self.input.project
      INSTANCE = self.input.instance
    }
  }
}

locals {
  proxy_files = var.proxy_enabled ? {
    "client.crt" = tls_self_signed_cert.power.cert_pem
    "client.key" = tls_private_key.power.private_key_pem
    "server.crt" = tls_self_signed_cert.proxy[0].cert_pem
    "server.key" = tls_private_key.proxy[0].private_key_pem
    "host.crt"   = var.lxd_server_certificate
    "nginx.conf" = <<-EOF
      server {
        listen 127.0.0.1:944 ssl;
        listen 10.203.240.10:944 ssl;
        server_name localhost;
        ssl_certificate /etc/nginx/lxd-power/server.crt;
        ssl_certificate_key /etc/nginx/lxd-power/server.key;
        ssl_client_certificate /etc/nginx/lxd-power/client.crt;
        ssl_verify_client on;
        location / {
          rewrite ^/1.0/virtual-machines(.*)$ /1.0/instances$1 break;
          proxy_pass https://10.203.240.1:8443;
          proxy_http_version 1.1;
          proxy_ssl_certificate /etc/nginx/lxd-power/client.crt;
          proxy_ssl_certificate_key /etc/nginx/lxd-power/client.key;
          proxy_ssl_trusted_certificate /etc/nginx/lxd-power/host.crt;
          proxy_ssl_verify on;
          proxy_ssl_name ${var.lxd_server_name};
          proxy_ssl_server_name on;
          proxy_read_timeout 600s;
        }
      }
    EOF
  } : {}
}

resource "lxd_instance_file" "proxy" {
  for_each           = nonsensitive(toset(keys(local.proxy_files)))
  instance           = data.lxd_instance.maas.name
  project            = data.lxd_instance.maas.project
  content            = local.proxy_files[each.key]
  target_path        = each.key == "nginx.conf" ? "/etc/nginx/conf.d/lxd-power-tls.conf" : "/etc/nginx/lxd-power/${each.key}"
  mode               = "0600"
  uid                = 0
  gid                = 0
  create_directories = true
  depends_on         = [terraform_data.proxy_package]
}

resource "terraform_data" "proxy_ready" {
  count            = var.proxy_enabled ? 1 : 0
  triggers_replace = sha256(jsonencode(local.proxy_files))
  depends_on       = [lxd_instance_file.proxy]
  lifecycle {
    replace_triggered_by = [lxd_instance_file.proxy]
  }
  provisioner "local-exec" {
    command = <<-EOF
      lxc exec --project maas-bird-network-lab maas -- sh -ceu '
        ip -j route get 10.203.240.10 | jq -e '\''.[0].type == "local" and .[0].dev == "lo"'\''
        test "$(sysctl -n net.ipv4.ip_unprivileged_port_start)" -gt 944
        nginx -t
        systemctl unmask nginx.service
        systemctl enable --now nginx.service
        systemctl reload nginx.service
      '
    EOF
  }
}
