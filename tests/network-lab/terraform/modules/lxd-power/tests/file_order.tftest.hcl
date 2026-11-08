mock_provider "lxd" {
  override_during = plan
  mock_data "lxd_instance" {
    defaults = {
      name = "maas", project = "lab", type = "virtual-machine"
      config = { "user.maas-bird-owner" = "network-lab", "limits.cpu" = "2", "limits.memory" = "8GiB" }
    }
  }
}
mock_provider "tls" { override_during = plan }
run "parallel_files_use_prepared_directory_and_private_modes" {
  command = plan
  assert {
    condition     = length(lxd_instance_file.proxy) == 6 && alltrue([for item in lxd_instance_file.proxy : item.create_directories == false && item.mode == "0600" && item.uid == 0 && item.gid == 0])
    error_message = "All TLS/proxy files must use the prepared directories, retaining root-only file permissions."
  }
}
