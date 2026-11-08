# dpu-attachments

Principal machine charm in the [MAAS BIRD monorepo](../../README.md).

Build from this directory with:

```bash
CHARMCRAFT_EXPERIMENTAL_MONOREPO=1 charmcraft pack --use-lxd
```

See the root README for prerequisites, configuration, relation contracts and
verification boundaries. This charm is not a subordinate.

The `backend` option defaults to `dpu`, which observes devlink representors,
OVS membership and LLDP on the DPU. Select `native-nic` explicitly to observe
ordinary host NICs. This backend runs on the same host as BIRD and requires
`lldpcli` plus native PCI/permanent-MAC discovery tools. It does not install
packages or configure addresses, forwarding, OVS or SR-IOV.

For native mode, `attachments` is a JSON array such as:

```json
[
  {
    "identity": {"scheme": "permanent-mac", "value": "02:00:00:00:00:01"},
    "function": {"controller": 0, "pf": 3},
    "id": "general-a",
    "domain": "general",
    "vlan": 3404,
    "uplink": "enp7s0",
    "chassis_id": "02:00:00:aa:00:01",
    "peer_link_local": "fe80::1"
  }
]
```

Use actual permanent MACs and native function coordinates. The PF and VF
coordinates come from PCI ancestry; an ordinary PF has no VF index. A native
devlink serial identity is also accepted when discovery supplies one. An
application may contain mappings for several hosts; a unit publishes only
cards present in its own complete native snapshot.

Readiness requires exactly one matching identity/function/VLAN, its actual
parent uplink, usable local IPv6 link-local addressing, both links up, and the
exact approved LLDP chassis set. LLDP on the parent does not prove VLAN
forwarding or ownership of the configured peer address; authenticated BGP,
BFD and packet checks remain necessary. MAAS supplies independent rack/TOR
authority to the BIRD consumer.

Known absence publishes withdrawn or non-ready attachments. Read failures
retain the last observation with Waiting status; they are not authoritative
empty snapshots. An explicitly empty policy withdraws all native attachments.
Unknown NUMA placement stays unknown and cannot satisfy a NUMA-specific context.

Unit and relation-scenario checks cover the native backend. Installation of
the packed charm and positive live routing acceptance have not yet been verified.
