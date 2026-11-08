mock_provider "juju" {
  override_during = plan
  mock_data "juju_model" {
    defaults = { uuid = "model-uuid" }
  }
}

run "omitted_constraints_preserve_legacy_deploy_hash" {
  command = plan
  variables {
    applications = { inventory = { charm = "maas-inventory", machine = "0", options = {} } }
  }
  assert {
    condition     = terraform_data.deploy.triggers_replace[0] == sha256(jsonencode({ inventory = { charm = "maas-inventory", machine = "0", options = {} } }))
    error_message = "Omitted constraints must preserve the exact legacy application hash."
  }
}

run "null_constraints_preserve_legacy_deploy_hash" {
  command = plan
  variables {
    applications = { inventory = { charm = "maas-inventory", machine = "0", options = {}, constraints = null } }
  }
  assert {
    condition     = terraform_data.deploy.triggers_replace[0] == sha256(jsonencode({ inventory = { charm = "maas-inventory", machine = "0", options = {} } }))
    error_message = "Null constraints must not cause an unrelated bundle replay."
  }
}

run "explicit_constraints_participate_in_deploy_hash" {
  command = plan
  variables {
    applications = { inventory = { charm = "maas-inventory", machine = "0", options = {}, constraints = "arch=amd64" } }
  }
  assert {
    condition     = terraform_data.deploy.triggers_replace[0] == sha256(jsonencode({ inventory = { charm = "maas-inventory", machine = "0", options = {}, constraints = "arch=amd64" } }))
    error_message = "Explicit application constraints must participate in declarative deployment."
  }
}

run "explicit_empty_constraints_are_not_normalized_away" {
  command = plan
  variables {
    applications = { inventory = { charm = "maas-inventory", machine = "0", options = {}, constraints = "" } }
  }
  assert {
    condition     = terraform_data.deploy.triggers_replace[0] == sha256(jsonencode({ inventory = { charm = "maas-inventory", machine = "0", options = {}, constraints = "" } }))
    error_message = "Only omitted/null constraints may be excluded from the deployment hash."
  }
}

run "other_null_fields_retain_legacy_hash_meaning" {
  command = plan
  variables {
    applications = { inventory = { charm = "maas-inventory", machine = "0", options = null, constraints = null } }
  }
  assert {
    condition     = terraform_data.deploy.triggers_replace[0] == sha256(jsonencode({ inventory = { charm = "maas-inventory", machine = "0", options = null } }))
    error_message = "Constraint normalization must not remove any other null application field."
  }
}
