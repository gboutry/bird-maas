# Platform and compatibility

## Platform

The lab requires Linux amd64, local Unix-socket LXD administration, KVM
support, storage pool `default` and an OVS carrier backend. LXD 5.21 and 6.10
API differences are handled by checking the boot-mode extension; older servers
use the guest Secure Boot setting. Container CPU allowances are not applied to
VMs.

The toolchain uses Terraform 1.17.1, Terragrunt 1.2.1, Juju 3.6.29 and Python
3.12 through uv. The provider build requires Go 1.27.3. The exact upstream
source, toolchain, patch and checksums are pinned in the
[MAAS provider manifest](../terraform/provider-patches/maas/source.json). The
downstream provider is `2.10.0-networklab.1`; substituting released 2.10.0
changes absent-resource and subnet-retention behavior.

FRR uses its official signed 10.7.1 package. The lab enables the signed
`canonical-openstack/openstack-caracal-dpu-support-staging` PPA for OVN
packages. Terraform installs the signing keys and APT preferences that exclude
other PPA packages. Installation requires an APT candidate at least `24.03.9`
and matching common/role package versions. APT selects the candidate from the
archive, so it can change with archive updates. Guest hosts use Noble HWE. The
MAAS control VM has its own kernel; containers share the host kernel.

## Addresses and BIRD configuration

| Model | Support |
| --- | --- |
| Loopback /32 identities with IPv6-link-local fast peers | The lab provisions identities and guards; BIRD manages dynamic routes |
| /32 on a fast interface | MAAS static linking is possible, but the current charm requires its GP/VTEP source on loopback; fast-only placement is rejected |
| /31 fast addresses with loopback sources | Fast addresses can coexist with loopback sources; peers still use IPv6 link-local addresses |
| IPv4-numbered BGP peers | The charm rejects these sessions and attachments; it needs support for these before integration |
| Repeated VID across fabrics | Keep fabric, parent and carrier identities distinct |
| Duplicate subnet CIDRs across fabrics | MAAS enforces global CIDR uniqueness |

MAAS has no loopback-creation API. Deployment customization creates the
loopbacks and static policy. Use explicit subnet-ID/interface links for /32
allocation. Explicit-IP reservation can fail to locate an exact /32 even when
subnet-based allocation succeeds. Dynamic ranges that include the first
address of a /31 or /32 encounter MAAS range validation. This is separate from
kernel and BIRD support. Support for /32s depends on the allocation operation.

For virtio NICs, discovery reads PCI ancestry and permanent MAC addresses. It
leaves VF/NUMA/DPU fields unset when it cannot observe them. The observer
validates real NICs and receive-only LLDP through the attachment relation.
Software VLAN children remain logical interfaces, without hardware VF
identity.

## Carriers and lifecycle

The profile requires VLAN trunks, moved/downstream MAC return delivery and
LLDP. OVS carriers support this when the host grants LLDP forwarding
permission. Ordinary LXD OVN ports are unsuitable for this profile's carrier
requirements; guest tenant OVN is independent.

MAAS power integration assumes one combined region/rack controller. The MAAS
VM runs an nginx proxy to LXD. It rewrites the driver's `/1.0/virtual-machines`
requests to `/1.0/instances`, verifies LXD's TLS certificate and uses restricted
client credentials. A separate remote rack controller needs its own frontend
security checks.

See [limitations](limitations.md) for known problems and the
[pipeline guide](../terraform/live/lab/README.md) for preparation and resource
ownership.
