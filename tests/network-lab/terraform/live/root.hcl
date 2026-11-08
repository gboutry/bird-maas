terraform_binary = "${get_repo_root()}/tests/network-lab/terraform/bin/terraform"

generate "backend" {
  path      = "backend.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<-EOF
    terraform {
      backend "local" {
        path = "${get_repo_root()}/tests/network-lab/runtime/terraform/${path_relative_to_include()}/terraform.tfstate"
      }
    }
  EOF
}

terraform {
  extra_arguments "bounded_parallelism" {
    commands  = ["apply", "plan", "destroy"]
    arguments = ["-parallelism=8"]
  }
}
