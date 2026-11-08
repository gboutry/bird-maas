terraform {
  required_version = ">= 1.6"
  required_providers {
    local = {
      source  = "hashicorp/local"
      version = "2.9.1"
    }
  }
}

variable "output_directory" {
  type = string
}

variable "charms" {
  type = map(string)
  validation {
    condition = alltrue([
      for name in keys(var.charms) : contains(["maas-inventory", "dpu-attachments", "bird"], name)
    ])
    error_message = "Only the three repository charm artifacts are supported."
  }
}

variable "applications" {
  type = map(object({
    charm       = string
    machine     = string
    options     = map(string)
    constraints = optional(string)
  }))
}

locals {
  used_charms = toset([for application in var.applications : application.charm])
  machine_ids = toset([for application in var.applications : application.machine])
}

resource "terraform_data" "artifact" {
  for_each         = local.used_charms
  triggers_replace = filesha256(var.charms[each.key])
}

resource "local_file" "charm" {
  for_each             = local.used_charms
  filename             = "${var.output_directory}/${each.key}"
  source               = var.charms[each.key]
  file_permission      = "0600"
  directory_permission = "0700"
  lifecycle {
    replace_triggered_by = [terraform_data.artifact[each.key]]
  }
}

resource "local_sensitive_file" "bundle" {
  filename             = "${var.output_directory}/bundle.yaml"
  file_permission      = "0600"
  directory_permission = "0700"
  content = yamlencode({
    applications = {
      for name, application in var.applications : name => merge({
        charm     = "./${application.charm}"
        num_units = 1
        to        = [application.machine]
        options   = application.options
      }, application.constraints == null ? {} : { constraints = application.constraints })
    }
    machines = { for machine in local.machine_ids : machine => {} }
  })
  depends_on = [local_file.charm]
}

output "bundle_path" {
  value = local_sensitive_file.bundle.filename
}

output "machine_mapping" {
  value = "existing,${join(",", [for machine in sort(tolist(local.machine_ids)) : "${machine}=${machine}"])}"
}
