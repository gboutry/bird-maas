# Design decisions

## Who manages what

Terraform owns LXD infrastructure, MAAS network declarations, private PKI,
service configuration and Juju resources. Terragrunt runs plans in dependency
order. Juju runs the repository charms on machines deployed by MAAS. The lab
uses real services and tests their packet paths.

The project labels its resources, checks immutable instance UUIDs and sets
resource caps. Administrative credentials, private keys, BGP passwords, state
and generated bundles stay in private storage. The inventory publisher
receives its MAAS credential through a Juju secret; BIRD consumes normalized
inventory. The power frontend has restricted LXD client permissions and
verifies upstream TLS.

## Separate forwarding domains

Loopback /32s keep each source address independent of a particular fast link.
IPv6-link-local sessions carry IPv4 NLRI over independent VLAN children.
Source-policy tables and unreachable guards keep access, replication and
general traffic out of management when their allowed paths disappear.

Host OVS terminates workload/Geneve ports. Linux routing selects the general
uplink; neither physical parent is assigned to a provider bridge. Separate OVN
gateway VMs connect to the provider network. Test EVPN transport, OVN gateway
selection and NAT forwarding separately.

Each management path has an IP-less access container and a separate downstream
carrier. Stopping a container leaves the guest NIC carrier up, so the bond
must detect the loss of forwarding. Both paths still share the upstream
management network and physical host.

## Host carriers

The carriers must pass VLAN trunks and LLDP, and deliver return traffic to
downstream MACs when those MACs move. Ordinary LXD OVN ports do not provide
the trunk and MAC behavior this topology needs, so the lab uses OVS carriers.
Tenant OVN runs inside the lab and is independent of LXD's carrier backend.
LLDP forwarding requires the exact-command
[host policy](../terraform/host-policy/README.md); Terraform reapplies the
forwarding setting when recreating the lab.

## Lifecycle

Guest files must be removed while their machines are still deployed. Juju
machine release can stop or erase a worker, so full teardown reverses the DAG
instead of deleting units in numeric order. The MAAS provider retains the
controller-created management subnet and handles absent fabric/VLAN resources.

Before checking a model UUID, the deployment refreshes Juju's model metadata.
After creating relations, it waits for the expected applications and units to
be Active with Idle agents. The wait renews its query subscription within a
fixed time budget so it can check fresh state. Route checks and packet tests
run separately.

The software model covers routed networking. Cumulus, DPU firmware, SR-IOV,
physical NUMA placement and offload performance need hardware testing.
[Compatibility](../docs/compatibility.md),
[limitations](../docs/limitations.md) and [validation](../docs/validation.md)
describe what the lab supports and what the tests cover.
