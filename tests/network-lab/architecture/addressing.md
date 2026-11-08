# Addresses and configuration

The lab management network is `10.203.241.0/24`, with gateway `10.203.241.1`.
MAAS allocates management addresses and runs DHCP. Look up controller and
worker addresses in Terraform outputs and MAAS machine records rather than
using legacy examples or remembered DHCP leases.

[Logical roles](../maas/network-model/logical-roles.json) define the role
VLANs, source-policy tables and rule priorities:

| Role | VLAN | Routing table | Rule priority | Identity block |
| --- | ---: | ---: | ---: | --- |
| access0 | 3400 | 4100 | 10000 | `10.205.20.0/24` |
| access1 | 3401 | 4101 | 10100 | `10.205.21.0/24` |
| replication0 | 3402 | 4200 | 10200 | `10.205.30.0/24` |
| replication1 | 3403 | 4201 | 10300 | `10.205.31.0/24` |
| general | 3404 | 4400 | 14000 | `10.205.10.0/24` |

Each worker has one loopback /32 per role. BIRD advertises it through both
VLAN children for that role. General identities are `10.205.10.20/32` through
`10.205.10.23/32`. Fast children remain IPv6-link-local-only for BGP; MAAS
records their IPv4 mode as `LINK_UP`. Gateway identities are `10.205.251.1/32`
and `10.205.251.2/32`; fabric loopbacks use `10.205.250.0/24`. Provider
VLAN/VNI 3405 is separate from these host transport identities.

MAAS has no API to create loopbacks. Deployment Netplan creates the loopback
addresses, source rules and static unreachable routes. Networkd installs these
with protocol tag 4; the BIRD charm's `policy-protocol` must match. BIRD
manages its daemon and dynamic role routes. It does not take over arbitrary
existing addresses or static rules.

Each role table has an unreachable default with metric 42760. The main table
also blocks unresolved `10.205.0.0/16` destinations. Managed sources must not
fall back to management's default route. Fast parents and VLANs are optional
for boot, disable router advertisements and retain IPv6 link-local addressing
where BGP needs it.

The management active-backup bond uses the management gateway as its ARP
monitor target, a 200-ms interval, active-port validation and disabled MII
monitoring. Bootstrap checks the running kernel settings as well as the
Netplan files. Reapplying Netplan can leave an existing bond's old monitoring
settings active.

Reusing a VLAN ID requires separate MAAS fabrics and carriers. The same VID on
two carriers does not join their broadcast domains. MAAS subnet CIDRs remain
globally unique across fabrics. See [compatibility](../docs/compatibility.md)
for interface /32, /31 and reservation constraints.
