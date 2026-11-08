mock_provider "lxd" {
  override_during = plan
  mock_data "lxd_instance" {
    defaults = { config = { "volatile.uuid" = "switch-uuid", "volatile.fast0.hwaddr" = "02:00:00:00:00:01" } }
  }
}
run "generic_default" {
  command = plan
  assert {
    condition     = var.cloud_init_timeout_seconds == 300
    error_message = "Generic FRR callers must retain the 300-second provisioning deadline."
  }
}
run "native_slow_download_deadline" {
  command = plan
  variables { cloud_init_timeout_seconds = 1200 }
  assert {
    condition     = var.cloud_init_timeout_seconds == 1200
    error_message = "The native bootstrap override must accept 1200 seconds."
  }
}
run "maximum_deadline" {
  command = plan
  variables { cloud_init_timeout_seconds = 3600 }
}
run "reject_zero" {
  command = plan
  variables { cloud_init_timeout_seconds = 0 }
  expect_failures = [var.cloud_init_timeout_seconds]
}
run "reject_negative" {
  command = plan
  variables { cloud_init_timeout_seconds = -1 }
  expect_failures = [var.cloud_init_timeout_seconds]
}
run "reject_fractional" {
  command = plan
  variables { cloud_init_timeout_seconds = 1.5 }
  expect_failures = [var.cloud_init_timeout_seconds]
}
run "reject_unbounded" {
  command = plan
  variables { cloud_init_timeout_seconds = 3601 }
  expect_failures = [var.cloud_init_timeout_seconds]
}
