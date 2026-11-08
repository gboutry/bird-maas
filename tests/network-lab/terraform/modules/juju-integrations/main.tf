terraform {
  required_providers {
    juju = { source = "juju/juju", version = "2.4.1" }
  }
}
variable "controller_addresses" { type = list(string) }
variable "controller_ca" { type = string }
variable "controller_username" { type = string }
variable "controller_password" {
  type      = string
  sensitive = true
}
variable "model_uuid" { type = string }
variable "worker_applications" { type = map(string) }
variable "wait_for_ready" {
  type    = bool
  default = false
}
variable "model" {
  type    = string
  default = ""
  validation {
    condition     = var.model == "" || can(regex("^[a-z][a-z0-9-]*:[a-z][a-z0-9/-]*$", var.model))
    error_message = "Use an explicit controller:model target for readiness."
  }
}
variable "client_directory" {
  type    = string
  default = ""
}
variable "deployment_generation" {
  type     = string
  default  = ""
  nullable = false
}
variable "worker_generations" {
  type    = map(string)
  default = {}
}
variable "secret_ids" {
  type = object({ maas = string, ca = string, bgp = map(string) })
}
provider "juju" {
  controller_addresses = join(",", var.controller_addresses)
  ca_certificate       = var.controller_ca
  username             = var.controller_username
  password             = var.controller_password
}
resource "juju_access_secret" "maas" {
  model_uuid   = var.model_uuid
  secret_id    = var.secret_ids.maas
  applications = ["inventory"]
}
resource "juju_access_secret" "ca" {
  model_uuid   = var.model_uuid
  secret_id    = var.secret_ids.ca
  applications = ["inventory"]
}
resource "juju_access_secret" "bgp" {
  for_each     = var.worker_applications
  model_uuid   = var.model_uuid
  secret_id    = var.secret_ids.bgp[each.key]
  applications = ["bird-${each.value}"]
}
resource "juju_integration" "inventory" {
  for_each   = var.worker_applications
  model_uuid = var.model_uuid
  application {
    name     = "inventory"
    endpoint = "inventory"
  }
  application {
    name     = "bird-${each.value}"
    endpoint = "inventory"
  }
}
resource "juju_integration" "attachments" {
  for_each   = var.worker_applications
  model_uuid = var.model_uuid
  application {
    name     = "nic-${each.value}"
    endpoint = "attachments"
  }
  application {
    name     = "bird-${each.value}"
    endpoint = "attachments"
  }
}
locals {
  client_directory = var.client_directory == "" ? pathexpand("~/.local/share/juju") : var.client_directory
  ready_count      = 1 + 2 * length(var.worker_applications)
  ready_query      = "life == \"alive\" && status == \"available\" && len(applications) == ${local.ready_count} && len(units) == ${local.ready_count} && forEach(applications, app => app.status == \"active\") && forEach(units, unit => unit.workload-status == \"active\" && unit.agent-status == \"idle\")"
  identity_check   = <<-SCRIPT
    "$JUJU" models --controller "$${MODEL%%:*}" --format=json >/dev/null
    "$JUJU" show-model "$MODEL" --format=json | jq -e --arg expected "$UUID" '[.[] | .["model-uuid"]] == [$expected]' >/dev/null
  SCRIPT
}
resource "terraform_data" "ready" {
  count = var.wait_for_ready ? 1 : 0
  triggers_replace = {
    model        = var.model, uuid = var.model_uuid, client = local.client_directory
    applications = var.worker_applications, deployment_generation = var.deployment_generation
    workers      = var.worker_generations, secrets = var.secret_ids
    grants = {
      maas = juju_access_secret.maas.id, ca = juju_access_secret.ca.id
      bgp  = { for name, grant in juju_access_secret.bgp : name => grant.id }
    }
    relations = {
      inventory   = { for name, relation in juju_integration.inventory : name => relation.id }
      attachments = { for name, relation in juju_integration.attachments : name => relation.id }
    }
  }
  depends_on = [juju_access_secret.maas, juju_access_secret.ca, juju_access_secret.bgp, juju_integration.inventory, juju_integration.attachments]
  lifecycle {
    precondition {
      condition     = var.model != "" && trimspace(var.deployment_generation) != "" && length(var.worker_applications) > 0
      error_message = "Readiness requires an explicit model, an actual deployment generation and at least one worker application pair."
    }
  }
  provisioner "local-exec" {
    interpreter = ["bash", "-euo", "pipefail", "-c"]
    command     = <<-SCRIPT
      ${local.identity_check}
      # Refresh native delta subscriptions within one readiness budget.
      deadline=$(($(date +%s) + 600))
      while :; do
        remaining=$((deadline - $(date +%s)))
        test "$remaining" -gt 0
        attempt=10
        if test "$remaining" -lt "$attempt"; then attempt=$remaining; fi
        if "$JUJU" wait-for model "$MODEL" --timeout="$attempt"s --summary=false --query="$QUERY"; then
          test "$(date +%s)" -lt "$deadline"
          break
        fi
        ${local.identity_check}
      done
      ${local.identity_check}
      test "$(date +%s)" -lt "$deadline"
    SCRIPT
    environment = {
      JUJU  = "/snap/juju/current/bin/juju", JUJU_DATA = local.client_directory
      MODEL = var.model, UUID = var.model_uuid, QUERY = local.ready_query
    }
  }
}
output "ready" { value = try(terraform_data.ready[0].id, null) }
