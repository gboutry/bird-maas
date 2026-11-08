terraform {
  required_version = ">= 1.7"
  required_providers {
    juju = { source = "juju/juju", version = "2.4.1" }
  }
}
variable "model" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*:[a-z][a-z0-9/-]*$", var.model))
    error_message = "Use an explicit controller:model target."
  }
}
variable "model_uuid" { type = string }
variable "client_directory" {
  type    = string
  default = ""
}
variable "output_directory" { type = string }
variable "charms" { type = map(string) }
variable "applications" {
  type = map(object({ charm = string, machine = string, options = map(string), constraints = optional(string) }))
  validation {
    condition     = alltrue([for name in keys(var.applications) : can(regex("^[a-z][a-z0-9-]*$", name))])
    error_message = "Application names must be native Juju names."
  }
}
locals {
  # Omitted constraints carry no intent that should replay a deployed bundle.
  application_intent = {
    for name, application in var.applications : name => {
      for key, value in application : key => value if key != "constraints" || value != null
    }
  }
  client_directory = var.client_directory == "" ? pathexpand("~/.local/share/juju") : var.client_directory
  juju             = "/snap/juju/current/bin/juju"
  controller_name  = split(":", var.model)[0]
  model_parts      = split("/", split(":", var.model)[1])
  controller       = yamldecode(file("${local.client_directory}/controllers.yaml")).controllers[local.controller_name]
  account          = sensitive(yamldecode(file("${local.client_directory}/accounts.yaml")).controllers[local.controller_name])
  identity_check   = <<-SCRIPT
    "$JUJU" models --controller "$${MODEL%%:*}" --format=json >/dev/null
    "$JUJU" show-model "$MODEL" --format=json | jq -e --arg expected "$UUID" '[.[] | .["model-uuid"]] == [$expected]' >/dev/null
  SCRIPT
}
provider "juju" {
  controller_addresses = join(",", local.controller.api-endpoints)
  ca_certificate       = local.controller.ca-cert
  username             = local.account.user
  password             = local.account.password
}
data "juju_model" "target" {
  name  = local.model_parts[length(local.model_parts) - 1]
  owner = length(local.model_parts) == 2 ? local.model_parts[0] : local.account.user
  lifecycle {
    postcondition {
      condition     = self.uuid == var.model_uuid
      error_message = "The authenticated model UUID differs from the declared target."
    }
  }
}
module "bundle" {
  source           = "../juju-workloads"
  output_directory = var.output_directory
  charms           = var.charms
  applications     = var.applications
}
moved {
  from = local_file.charm
  to   = module.bundle.local_file.charm
}
moved {
  from = local_sensitive_file.bundle
  to   = module.bundle.local_sensitive_file.bundle
}
moved {
  from = terraform_data.artifact
  to   = module.bundle.terraform_data.artifact
}
resource "terraform_data" "target" {
  triggers_replace = { model = var.model, uuid = data.juju_model.target.uuid, client = local.client_directory }
  provisioner "local-exec" {
    command     = "set -eu\n${local.identity_check}"
    environment = { JUJU = local.juju, JUJU_DATA = local.client_directory, MODEL = var.model, UUID = var.model_uuid }
  }
}
resource "terraform_data" "application" {
  for_each = var.applications
  input = {
    app    = each.key, model = var.model, uuid = var.model_uuid,
    client = local.client_directory, juju = local.juju,
    query  = "forEach(applications, app => app.name != ${jsonencode(each.key)})"
  }
  triggers_replace = var.model_uuid
  depends_on       = [terraform_data.target]
  provisioner "local-exec" {
    when        = destroy
    interpreter = ["bash", "-euo", "pipefail", "-c"]
    command     = <<-SCRIPT
      "$JUJU" models --controller "$${MODEL%%:*}" --format=json >/dev/null
      "$JUJU" show-model "$MODEL" --format=json | jq -e --arg expected "$UUID" '[.[] | .["model-uuid"]] == [$expected]' >/dev/null
      present=$("$JUJU" status --model="$MODEL" --format=json | jq -r --arg app "$APP" '.applications | has($app)')
      if test "$present" = true; then
        "$JUJU" remove-application "$APP" --model="$MODEL" --no-prompt
      fi
      "$JUJU" wait-for model "$MODEL" --timeout=5m --summary=false --query="len(applications) == 0 || ($QUERY)"
    SCRIPT
    environment = {
      JUJU  = self.input.juju, JUJU_DATA = self.input.client,
      MODEL = self.input.model, UUID = self.input.uuid, APP = self.input.app, QUERY = self.input.query
    }
  }
}
resource "terraform_data" "deploy" {
  triggers_replace = [sha256(jsonencode(local.application_intent)), var.model_uuid, var.output_directory]
  depends_on       = [terraform_data.application, module.bundle]
  provisioner "local-exec" {
    command = "set -eu\n${local.identity_check}\n\"$JUJU\" deploy \"$BUNDLE\" --model=\"$MODEL\" --map-machines=\"$MAPPING\""
    environment = {
      JUJU   = local.juju, JUJU_DATA = local.client_directory, MODEL = var.model, UUID = var.model_uuid,
      BUNDLE = module.bundle.bundle_path, MAPPING = module.bundle.machine_mapping
    }
  }
}
resource "terraform_data" "refresh" {
  for_each         = var.applications
  triggers_replace = [filesha256(var.charms[each.value.charm]), var.model_uuid, each.value.charm]
  depends_on       = [terraform_data.deploy]
  provisioner "local-exec" {
    command = "set -eu\n${local.identity_check}\n\"$JUJU\" refresh \"$APP\" --model=\"$MODEL\" --path=\"$ARCHIVE\""
    environment = {
      JUJU = local.juju, JUJU_DATA = local.client_directory, MODEL = var.model, UUID = var.model_uuid,
      APP  = each.key, ARCHIVE = "${var.output_directory}/${each.value.charm}"
    }
  }
}
output "bundle_path" { value = module.bundle.bundle_path }
output "machine_mapping" { value = module.bundle.machine_mapping }
output "deployment_generation" {
  value = sha256(jsonencode({
    deploy  = terraform_data.deploy.id
    refresh = { for name, refresh in terraform_data.refresh : name => refresh.id }
  }))
}
