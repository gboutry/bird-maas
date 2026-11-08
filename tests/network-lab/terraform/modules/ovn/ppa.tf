variable "use_dpu_support_ppa" {
  description = "Use the Canonical OpenStack Caracal DPU staging PPA for OVN packages only."
  type        = bool
  default     = false
}

locals {
  ppa_targets = var.use_dpu_support_ppa ? merge(
    { (var.central_instance) = { uuid = data.lxd_instance.central.config["volatile.uuid"], role = "central" } },
    { for name, client in local.active : name => { uuid = client.uuid, role = "host" } },
  ) : {}
  ppa_configuration = {
    key = {
      path    = "/usr/share/keyrings/network-lab-canonical-openstack.asc"
      content = file("${path.module}/canonical-openstack.asc")
    }
    source = {
      path    = "/etc/apt/sources.list.d/network-lab-ovn.sources"
      content = "Types: deb\nURIs: https://ppa.launchpadcontent.net/canonical-openstack/openstack-caracal-dpu-support-staging/ubuntu\nSuites: noble\nComponents: main\nSigned-By: /usr/share/keyrings/network-lab-canonical-openstack.asc\n"
    }
    preferences = {
      path    = "/etc/apt/preferences.d/network-lab-ovn"
      content = "Package: ovn-common ovn-host ovn-central\nPin: release o=LP-PPA-canonical-openstack-openstack-caracal-dpu-support-staging\nPin-Priority: 500\n\nPackage: *\nPin: release o=LP-PPA-canonical-openstack-openstack-caracal-dpu-support-staging\nPin-Priority: -1\n"
    }
  }
  ppa_files = merge({}, [for name, target in local.ppa_targets : {
    for kind, file in local.ppa_configuration : "${name}/${kind}" => merge(file, { instance = name })
  }]...)
}

resource "lxd_instance_file" "ppa" {
  for_each           = local.ppa_files
  instance           = each.value.instance
  project            = var.project
  target_path        = each.value.path
  content            = each.value.content
  mode               = "0644"
  uid                = 0
  gid                = 0
  create_directories = true
  depends_on         = [terraform_data.central_preflight, terraform_data.client_preflight]
}

resource "terraform_data" "ppa" {
  for_each         = local.ppa_targets
  triggers_replace = concat([each.value.uuid, sha256(jsonencode(local.ppa_configuration))], try([var.worker_generations[each.key]], []))
  depends_on       = [lxd_instance_file.ppa]
  provisioner "local-exec" {
    command = <<-SCRIPT
      set -eu
      lxc query "/1.0/instances/$INSTANCE?project=$PROJECT" | jq -e --arg uuid "$UUID" '.config["user.maas-bird-owner"] == "network-lab" and .config["volatile.uuid"] == $uuid' >/dev/null
      lxc exec --project "$PROJECT" "$INSTANCE" -- sh -ceu '
        . /etc/os-release
        test "$ID" = ubuntu && test "$VERSION_ID" = 24.04
        printf "%s\n" "$2" | sha256sum -c -
        export DEBIAN_FRONTEND=noninteractive
        apt-get -o APT::Update::Error-Mode=any update -qq
        candidate=$(LC_ALL=C apt-cache policy "ovn-$1" | awk "/Candidate:/ {print \$2}")
        dpkg --compare-versions "$candidate" ge 24.03.9
        apt-get -o DPkg::Lock::Timeout=600 --no-remove --no-install-recommends install -y ovn-common "ovn-$1"
        test "$(dpkg-query -W -f=\$\{Version\} "ovn-$1")" = "$candidate"
        test "$(dpkg-query -W -f=\$\{Version\} ovn-common)" = "$candidate"
        dpkg-query -W ovn-common "ovn-$1"
        if test "$1" = central; then
          systemctl restart ovn-central
          ovn-nbctl --timeout=30 show >/dev/null
          ovn-sbctl --timeout=30 show >/dev/null
        else
          systemctl restart ovn-controller
        fi
      ' network-lab "$ROLE" "$CHECKSUMS"
    SCRIPT
    environment = {
      PROJECT   = var.project, INSTANCE = each.key, UUID = each.value.uuid, ROLE = each.value.role
      CHECKSUMS = join("\n", [for file in local.ppa_configuration : "${sha256(file.content)}  ${file.path}"])
    }
  }
}
