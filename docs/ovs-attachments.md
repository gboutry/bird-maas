# DPU LACP and local OVS internal attachments

BIRD uses the same routing policy on a host and a DPU. Its local attachment
discovery has two explicit backends: PCI functions, and OVS internal interfaces.
An OVS internal interface is a local kernel object, not a host VF with invented
PCI coordinates. BIRD must run on the machine and in the kernel namespace that
owns that OVS instance. A host BIRD unit cannot discover a DPU's internal ports.

## Physical LLDP with a retained Linux LACP bond

The attachment observer normally expects the physical `uplink` to be an OVS
interface. Where OVS-DOCA instead uses an existing kernel LACP aggregate, declare
the aggregate explicitly:

```json
{
  "serial": "REPLACE-WITH-CARD-SERIAL",
  "id": "host-a-public0-tor1",
  "function": {"controller": 1, "pf": 0, "vf": 55},
  "host_function": {"controller": 0, "pf": 0, "vf": 55},
  "domain": "ceph-public",
  "vlan": 3500,
  "uplink": "p0",
  "uplink_bond": "bond0",
  "chassis_id": "02:00:00:01:00:01",
  "peer_link_local": "fe80::1"
}
```

The observer checks LLDP on `p0`, its exact kernel master `bond0`, mode802.3ad,
distinct configured members and native sysfs ancestry to the same devlink card
serial for every member. It then checks OVS membership using `bond0`. A foreign
card, another master or another bond mode refuses readiness. Without
`uplink_bond`, the previous direct-uplink behavior remains: no bond is inferred
or adopted from its name.

This proves attachment structure, not that every member is currently forwarding
or that a particular TOR is the physical exit. The two members can reach different
TOR chassis in an MLAG pair. Requiring both members to be up would defeat
single-member failover; BGP/BFD and packet tests determine adjacency health.
NUMA preference continues to describe host-to-card locality. It does not select
a physical member inside LACP. No hardware-offload result follows from readiness.

## An internal interface for the DPU's own routed underlay

The provisioning owner creates the OVS internal ports, access tags, MTU,
link-local addresses, loopback endpoint and persistent source guards. The charms
observe those objects and manage BIRD; they do not create or modify forwarding.

The DPU observer's `attachments` entry uses `ovs_port` instead of both `function`
and `host_function`:

```json
{
  "serial": "REPLACE-WITH-CARD-SERIAL",
  "id": "dpu-a-underlay-tor1",
  "ovs_port": {"bridge": "br-data", "port": "u3580"},
  "domain": "underlay",
  "vlan": 3580,
  "uplink": "p0",
  "uplink_bond": "bond0",
  "chassis_id": "02:00:00:01:00:01",
  "peer_link_local": "fe80::1"
}
```

The co-located BIRD application's `local-ovs-ports` option supplies the local
operator upper bound. It defaults to `[]`; normal host discovery does not query
OVS. Each DPU unit selects only entries for card serials present on that unit.

```json
[
  {
    "serial": "REPLACE-WITH-CARD-SERIAL",
    "bridge": "br-data",
    "port": "u3580",
    "uplink": "p0",
    "uplink_bond": "bond0"
  }
]
```

The backend takes a native read-only OVSDB snapshot and requires:

- One Port on the named Bridge, with exactly one same-named Interface.
- Interface type `internal`, installed positive ofport/ifindex and no error.
- One access VLAN tag, no trunk list, and no conflicting VLAN mode.
- A same-card physical uplink, with the explicit LACP checks when declared.
- A matching local kernel ifindex and one usable IPv6 link-local adjacency.

The provider publishes observed Bridge, Port and Interface UUIDs, kernel ifindex
and VLAN in `ovs_port`. The consumer independently checks those against its
explicit local discovery. Relations retain `function: null` for logical ports.
A physical selector cannot match a logical port, or vice versa. A mixed selector
is invalid. The underlay routing context can use `numa_node: null`; discovery does
not pretend a DPU internal port is a host NUMA-local VF.

## Revocation, startup and rollback

Accepted sessions retain the logical selector and native object identities.
Changing the bridge, VLAN or UUID is new authority, even when the interface name
is unchanged. A complete local observation that no longer contains the accepted
binding removes that session from the durable restrictive floor before a new
grant. Unknown discovery is not treated as an authoritative empty observation.
Removing the local opt-in list also removes the old logical sessions.

The installed stdlib-only startup checker re-reads the OVS binding and kernel
ifindex. Recreated ports cannot inherit accepted boot authority merely by using
the same name. It remains fail closed if OVS is unavailable. Static source-wide
rules and unreachable-default guards are still checked before reconciling even
an invalid proposed configuration.

There is no continuous OVS object lease. A port changed after validation while
the charm is not running can affect the live daemon until a subsequent reconcile
or restart. BIRD's native timed configuration rollback does not undo external OVS
changes. Network provisioning must retain its own change protection and lifecycle
ordering; these checks are not a replacement for that owner.

## Verification boundary

Unit and property tests exercise native-output parsing, physical/logical selector
isolation, UUID replacement restrictions and startup admission. The separate
OVSDB acceptance test runs real tools against its own temporary database and
explicit socket, without vswitchd, a kernel datapath or a production connection:

```sh
OVS_BINARY_DIR=/path/to/ovs/bin \
OVS_SCHEMA=/path/to/vswitch.ovsschema \
uv run --locked pytest tests/ovsdb -q
```

It is intentionally separate from `make native`, so generic BIRD development
does not acquire an implicit OVS test prerequisite. Native BIRD parsing and
existing namespace forwarding tests cover other boundaries. None establishes
physical DOCA forwarding, routed Geneve offload, a real MAAS allocation or a
production cutover. Those remain physical acceptance requirements.
