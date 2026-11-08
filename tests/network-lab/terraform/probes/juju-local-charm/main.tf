terraform {
  required_version = ">= 1.11"
  required_providers {
    juju = {
      source  = "juju/juju"
      version = "2.4.1"
    }
  }
}

variable "controller_connection_file" {
  type = string
}

variable "charm_path" {
  type = string
}

locals {
  controller = sensitive(jsondecode(file(var.controller_connection_file))["network-lab"])
}

provider "juju" {
  controller_addresses = join(",", local.controller.details.api-endpoints)
  ca_certificate       = local.controller.details.ca-cert
  username             = local.controller.account.user
  password             = local.controller.account.password
}

data "juju_model" "probe" {
  name  = "network-lab-tf-probe"
  owner = local.controller.account.user
  lifecycle {
    precondition {
      condition     = contains(local.controller.details.api-endpoints, "10.203.240.12:17070")
      error_message = "The probe requires the dedicated lab controller."
    }
  }
}

resource "juju_application" "probe" {
  name       = "local-charm-probe"
  model_uuid = data.juju_model.probe.uuid
  units      = 0
  charm {
    name = var.charm_path
    base = "ubuntu@24.04"
  }
}
