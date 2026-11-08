# Two-rack network lab

This lab uses MAAS to install four Ubuntu Noble HWE workers and Juju to deploy
BIRD on them. Two spines and four ToRs run FRR. Separate gateway VMs connect
OVN to the provider network.

Each worker has two fast interfaces, with five VLAN children on each to model
VF roles. BGP carries IPv4 routes over IPv6 link-local sessions. Geneve tunnels
use stable /32 loopback addresses. Management uses an active-backup bond through
two access switches. The switches share an upstream network and a physical host.

The LXD project is `maas-bird-network-lab-tf`. It has eight VMs and eight
containers, with 16 CPUs and 38 GiB of RAM allocated. The project memory limit
is 56 GiB.

## Deploy

Start with the [setup guide](terraform/live/lab/README.md). It covers host
packages, images, charm and provider builds, credentials, and temperature
monitoring. Once those are ready, run Terragrunt from the repository root:

```sh
cd tests/network-lab/terraform/live/lab
terragrunt run --all --parallelism 2 --non-interactive -- apply -input=false -parallelism=4
```

The guide lists the environment settings for the OVS client and FRR CPU quota.
Terragrunt runs seventeen Terraform configurations in dependency order.

For the same deployment with automatic credential setup, preflight checks and
temperature monitoring, run this from the repository root instead:

```sh
make deploy-network-lab
```

The Make target runs a local Python wrapper around Terragrunt. The wrapper and
monitor are not installed as services. Host setup and artifact builds are still
separate steps. Use the setup guide to inspect, destroy or recover a deployment;
it also shows how to use its Juju controller.

## Read more

- [Topology and resource allocation](architecture/topology.md)
- [Addresses and who configures them](architecture/addressing.md)
- [Failure domains](architecture/failure-domains.md)
- [Design choices](architecture/decisions.md)
- [FRR and Cumulus](architecture/switch-platform.md)
- [MAAS and network compatibility](docs/compatibility.md)
- [Known problems and test limits](docs/limitations.md)
- [Running the tests](docs/validation.md)

The lab covers host routing, storage roles, Geneve, provider EVPN and OVN
gateways. VLAN children model the PF/VF dependencies, but cannot test SR-IOV,
DPU offload or NUMA placement. Cumulus, physical capacity and availability need
their own hardware tests.

The [older comparison fixtures](docs/legacy-lab.md) use a separate project,
manual Juju agents and a nested fabric. Keep their state separate from this
Terraform deployment. Credentials, generated files and test output stay under
ignored runtime directories.
