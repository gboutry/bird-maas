# Deploy the network lab

The lab has seventeen Terraform configurations. Terragrunt runs them in the
order needed to create LXD machines, configure MAAS and bootstrap Juju. You can
call Terragrunt directly or use the Make wrapper to handle credentials and
temperature monitoring. Some guest setup uses cloud-init and shell commands
from Terraform provisioners.

The profile uses sixteen instances, 16 CPUs and 38 GiB of RAM. The project
memory limit is 56 GiB. See the [topology](../../../architecture/topology.md),
[compatibility notes](../../../docs/compatibility.md) and
[known problems](../../../docs/limitations.md) for the layout and limits.

## Prepare the host

Use Linux amd64, local LXD access, working KVM support and an LXD storage pool
named `default`. The lab uses `nlab-tf-*` network names and `10.203.241.0/24` for
management; check for conflicts first. Charm builds need capacity in addition
to the lab's allocation.

Install Terraform 1.17.1, Terragrunt 1.2.1, the Juju 3.6.29 snap, uv/Python 3.12,
Git, make, jq, curl, OpenSSL and Go 1.27.3. Downloads need network access.
The provider build requires the Go version in its
[manifest](../../provider-patches/maas/source.json).
The Terraform wrapper uses `/snap/bin/terraform` unless `TF_BINARY` names
another executable by absolute path. Juju uses `/snap/juju/current/bin/juju`.

LXD needs an OVS backend, either distribution OVS or the existing MicroOVN
installation. Guest OVN does not require host tenant networking. MAAS needs an
authenticated LXD HTTPS listener on port 8443, reachable from management.
Set that up separately; deployment does not install host packages or change
the listener.

LLDP forwarding needs sudo permission for the selected OVS executable.
Use full sudo access or install the [restricted policy](../../host-policy/README.md)
for the account running Terraform. Provisioners call `sudo -n`.

Import the pinned Ubuntu release images into LXD's default image project.
Run this from the repository root:

```sh
lab_vm_image=a80e9c9fd0d484dec024da3e9e264bbad4ce159b5312d6e149ef6ad532e18e43
lab_container_image=12a0de07e1683f41b169af0612897eae462efdeb84322281e91724c97e32fe0d
lxc image info "$lab_vm_image" --project default >/dev/null 2>&1 || \
    lxc image copy "ubuntu:$lab_vm_image" local: --vm --target-project default
lxc image info "$lab_container_image" --project default >/dev/null 2>&1 || \
    lxc image copy "ubuntu:$lab_container_image" local: --target-project default
```

## Build the charms and provider

Build from a clean checkout so Charmcraft cannot include state or credentials.
Set builder resource limits and monitor temperature before building; the pack
command does neither. Two CPUs and 8 GiB per builder are a starting budget,
separate from the running lab. From the repository root:

```sh
make pack-network-lab build-network-lab-provider
```

Packing writes amd64 archives to `tests/network-lab/runtime/charms`.
The provider build writes `2.10.0-networklab.1` to a local mirror. Its
[README](../../provider-patches/maas/README.md) describes the patch and maintenance.

The Terraform wrapper uses `runtime/terraform/providers/terraform.rc` when
`TF_CLI_CONFIG_FILE` is unset. If you supply your own configuration, include
the same mirror. Global configuration stays unchanged. Build artifacts can
be shared; each deployment needs its own state and credentials.

## Apply with Terragrunt

Create the MAAS password from the repository root. These commands keep an
existing password and reject symlinks:

```sh
set -eu
credential_dir=tests/network-lab/runtime/terraform/maas-credentials
test ! -L "$credential_dir"
install -d -m 0700 "$credential_dir"
test ! -L "$credential_dir/admin-password"
if [ ! -e "$credential_dir/admin-password" ]; then
    (umask 077; set -C; openssl rand -hex 32 > "$credential_dir/admin-password")
fi
test -f "$credential_dir/admin-password"
chmod 0600 "$credential_dir/admin-password"
LC_ALL=C grep -Eq '^[A-Za-z0-9_-]{32,}$' "$credential_dir/admin-password"
```

Run the temperature monitor in one terminal on the LXD host:

```sh
umask 077
uv run --locked python tests/network-lab/scripts/monitor.py \
  --project maas-bird-network-lab-tf --ceiling-c 75 --interval 2 --latch-stop \
  --output tests/network-lab/runtime/terraform/bootstrap-monitor.jsonl
```

Wait for its first sample. Check that it is recent, contains temperatures
and has `thermal_stop` set to false. In a second terminal, run the apply below.
The OVS path shown is for distribution OVS; omit that export when using the
MicroOVN default. With full sudo access, run `sudo -v` in this terminal just
before applying.

```sh
export TF_VAR_cpu_allowance=25%
# Distribution OVS only; match the administrator's sudo permission:
export TF_VAR_ovs_vsctl_path=/usr/bin/ovs-vsctl
cd tests/network-lab/terraform/live/lab
terragrunt run --all --parallelism 2 --non-interactive -- apply -input=false -parallelism=4
```

The 25% allowance applies to FRR containers; VM allowances are unchanged.
Keep the monitor running while commissioning or installation continues inside
guests, even if Terraform exits. Terraform parallelism does not limit that
background work.

At 75°C the monitor stops lab instances. The trip stays latched, so it will
also stop instances that appear later, even after the host cools. Inspect the
host and restart the monitor before resuming. Missing samples cannot protect
the host. Authentication/server errors do not count as an absent project.

For an existing deployment with matching source and dependency outputs,
inspect the plan from the repository root:

```sh
cd tests/network-lab/terraform/live/lab
terragrunt run --all --parallelism 2 -- plan -input=false
```

This checks changes to the existing lab. Testing fresh creation requires an
empty deployment. Terragrunt follows dependencies, not stage numbers. Run one
deployment operation at a time and use real dependency outputs.

To inspect Juju, use the client files created for this lab:

```sh
export JUJU_DATA="$(git rev-parse --show-toplevel)/tests/network-lab/runtime/terraform/juju-client"
juju status --model=nlab-tf:network-lab
```

## Optional Make wrapper

From the repository root:

```sh
make deploy-network-lab
```

This runs the same apply through `scripts/deploy_network_lab.py`. It checks
artifacts against source, matches live instance UUIDs against private state,
creates the password if needed, and starts the monitor. It rejects mismatched
state and the older shared-carrier profile. Host setup and builds remain
separate steps.

The wrapper and monitor are local processes, not installed services. BIRD's
Python pre-start policy check belongs to the charm. The older Python setup
services live with the legacy fixtures and are not part of this deployment.

Run the wrapper on the LXD host. The default `local` remote must use `unix://`
so readings come from the machine doing the work. Stop a separate monitor
before using the wrapper; it refuses to start a second one.

It uses two concurrent Terragrunt units, four Terraform operations per unit,
a 75°C stop threshold and a 25% FRR CPU allowance. It chooses an installed
MicroOVN OVS client or `/usr/bin/ovs-vsctl`. Explicit Terraform binary/config,
OVS and quota settings take precedence. It rejects other `TF_VAR_*` values,
injected Terraform CLI arguments and Terragrunt redirect variables. Use the
direct commands for a custom profile.

On success, it stops the monitor. On failure or interruption, it cancels the
apply process group and keeps the monitor in the foreground because guest
installation may still be running. Read the private output, wait for that work
to stop, then press Ctrl-C to stop the monitor and return the failure status.
A dead monitor or invalid readings cause the wrapper to cancel deployment.

## State and teardown

State, credentials, generated bundles, plans and backups belong under ignored
`tests/network-lab/runtime`. Keep matching source and artifacts. Use a clean
worktree for future charm builds; private files must stay out of commits and
Charmcraft sources. The wrapper creates private credentials and rejects symlinks.

An older shared-carrier deployment needs a teardown at its matching source
before creating this profile. Adding access containers before reducing the
controller allocation can exceed the CPU limit. Finish any required worker-file
ownership handoff first. Terraform removed/moved transitions need Terraform
1.7 or later; destroy does not perform the handoff.

From the pipeline directory, preview destruction:

```sh
terragrunt run --all --parallelism 2 --non-interactive -- plan -destroy -input=false
```

Check the project, instance UUIDs and network names. Back up the controllers
and state before deleting resources. Then destroy the reviewed lab:

```sh
terragrunt run --all --parallelism 2 --non-interactive -- destroy -input=false -parallelism=2
```

Terragrunt reverses the dependency order. Worker files must be removed before
Juju releases their machines. Stage 50 adds `-refresh=false` only for destroy,
because Juju may already have removed empty machines. Apply and plan still
refresh. Teardown needs the patched MAAS provider to retain the controller's
management subnet and handle fabrics/VLANs that MAAS has already deleted.

Afterward, check that backends are empty and the lab's instances, networks,
project and power certificate are gone. Leave other resources in place.
A readable backup is not proof that it can be restored.

An interrupted bootstrap may have no outputs for downstream units. Check each
unit's result and state: interruption can return zero despite failed units.
Filter the Terragrunt queue only after proving the excluded units have no
resources. Do not reuse another run's filter or edit state to hide objects.

## Terraform units

| Stage | What it configures |
| --- | --- |
| 00 | Project and OVS networks for management, access and fabric links |
| 10 | FRR containers and LLDP identity |
| 15 | Management access bridges |
| 20 | MAAS controller, TLS and credentials |
| 25 | Fabrics, VLANs, DHCP and Noble HWE images |
| 26 | LXD power credentials and TLS frontend |
| 30 | Blank worker/controller VMs and their immutable UUIDs |
| 35 | MAAS enrollment and commissioning |
| 40 | Interface imports by MAC, bonds, VLANs and rack tags |
| 45 | MAAS-backed Juju controller and its client files |
| 50 | Model, machine placement and host bootstrap configuration |
| 60 | Juju secrets and separate BGP passwords |
| 65 | FRR routing tables, networkd configuration and reloads |
| 70 | Repository charms and worker configuration files |
| 80 | Relations, secret access and application readiness |
| 85 | Gateway VMs, FRR and provider OVS |
| 90 | OVN central, TLS/RBAC, router/NAT and chassis checks |

Stage 30 waits for the access bridges, and stage 65 needs the gateway outputs.
Stage 80 waits up to ten minutes for the expected UUID, application/unit counts,
Active workloads and Idle agents. Each query waits up to ten seconds before
the next attempt. OVN separately requires its route check to pass within
180 seconds.

The lab gives FRR twenty minutes for first boot and downloads. The reusable
module defaults to five minutes and lets callers change it. OVN installation
waits for the APT frontend lock; it does not delete lock files.

Workers use ARP checks every 200 ms on the active management port, with MII
monitoring disabled. Bootstrap applies and checks both Netplan and kernel
settings. Worker and gateway fast ports receive LLDP without advertising;
admission requires the expected ToR chassis and port.

See [validation](../../../docs/validation.md) for test commands,
[compatibility](../../../docs/compatibility.md) for addressing, OVS and power
setup, and [limitations](../../../docs/limitations.md) for known problems and
TLS renewal.
