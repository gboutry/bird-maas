mock_provider "lxd" {
  override_during = plan
  mock_data "lxd_instance" {
    defaults = {
      config = {
        "volatile.uuid"        = "instance-uuid"
        "user.maas-bird-owner" = "network-lab"
        "limits.cpu"           = "2"
        "limits.memory"        = "8GiB"
      }
    }
  }
}
mock_provider "tls" { override_during = plan }

run "absent_generations_preserve_legacy_triggers" {
  command = plan
  assert {
    condition     = terraform_data.client_preflight["probe-alt"].triggers_replace == "instance-uuid"
    error_message = "Absent generation must retain the original scalar UUID trigger."
  }
  assert {
    condition     = length(terraform_data.ppa["probe-alt"].triggers_replace) == 2 && length(terraform_data.chassis["probe-alt"].triggers_replace) == 5
    error_message = "Absent generation must retain existing installer trigger shapes."
  }
}

run "compute_generation_is_scoped" {
  command = plan
  variables {
    worker_generations = { probe = "model/1", probe-alt = "model/2" }
  }
  assert {
    condition     = terraform_data.client_preflight["probe-alt"].triggers_replace == "instance-uuid/model/2" && terraform_data.ppa["probe-alt"].triggers_replace[2] == "model/2" && terraform_data.chassis["probe-alt"].triggers_replace[5] == "model/2"
    error_message = "Compute deployment generation must reach preflight and both native installers."
  }
  assert {
    condition     = terraform_data.client_preflight["gateway-r1"].triggers_replace == "instance-uuid" && length(terraform_data.ppa["gateway-r1"].triggers_replace) == 2 && length(terraform_data.chassis["gateway-r1"].triggers_replace) == 5 && length(terraform_data.ppa["maas"].triggers_replace) == 2
    error_message = "Compute generations must not alter gateway or central lifecycle triggers."
  }
}

run "same_uuid_new_machine_changes_only_its_generation" {
  command = plan
  variables {
    worker_generations = { probe = "model/1", probe-alt = "model/3" }
  }
  assert {
    condition     = terraform_data.client_preflight["probe-alt"].triggers_replace == "instance-uuid/model/3" && terraform_data.ppa["probe-alt"].triggers_replace[2] == "model/3" && terraform_data.chassis["probe-alt"].triggers_replace[5] == "model/3"
    error_message = "A new machine on the same UUID must change all compute installation triggers."
  }
  assert {
    condition     = terraform_data.client_preflight["probe"].triggers_replace == "instance-uuid/model/1" && terraform_data.ppa["probe"].triggers_replace[2] == "model/1" && terraform_data.chassis["probe"].triggers_replace[5] == "model/1" && terraform_data.client_preflight["gateway-r1"].triggers_replace == "instance-uuid"
    error_message = "The other compute and gateway must retain their deployment identities."
  }
}

run "empty_generation_is_rejected" {
  command = plan
  variables {
    worker_generations = { probe-alt = "" }
  }
  expect_failures = [var.worker_generations]
}

run "gateway_generation_is_rejected" {
  command = plan
  variables {
    worker_generations = { gateway-r1 = "model/7" }
  }
  expect_failures = [terraform_data.central_preflight]
}

run "central_generation_is_rejected" {
  command = plan
  variables {
    worker_generations = { maas = "model/7" }
  }
  expect_failures = [terraform_data.central_preflight]
}
