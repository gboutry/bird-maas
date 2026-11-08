locals {
  networkd_owner = "[Network]\nManageForeignRoutes=no\n"
}
provider "lxd" {
  remote {
    name    = "local"
    address = "unix://"
  }
}
# Guest files are adopted by stage 70 before application lifecycle ownership ends.
removed {
  from = lxd_instance_file.networkd
  lifecycle { destroy = false }
}
removed {
  from = terraform_data.networkd
  lifecycle { destroy = false }
}
