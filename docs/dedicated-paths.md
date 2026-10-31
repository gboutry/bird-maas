# Dedicated VF paths and a NUMA-neutral context

A routing domain can share its TOR sessions between contexts, or give each
context a dedicated pair. The ten-VF host layout uses dedicated pairs:

| Pair (one VF on each card) | Purpose | Preferred attachment |
|---|---|---|
| VF55 | Ceph public NUMA0 VIP | NUMA0 |
| VF56 | Ceph public NUMA1 VIP | NUMA1 |
| VF57 | Ceph replication NUMA0 VIP | NUMA0 |
| VF58 | Ceph replication NUMA1 VIP | NUMA1 |
| VF59 | General host traffic | No NUMA preference |

These VF numbers are a proposed RB3 reservation, not universal hardware defaults.
Each host needs ten distinct interfaces/functions. Dedicated pairs do not imply
a bandwidth reservation on the shared physical ports.

## Select paths explicitly

Set `attachment_ids` on a context to the exact attachment/session IDs it may use:

```json
{
  "id": "numa0",
  "machine_id": "example-host-a",
  "numa_node": 0,
  "attachment_ids": ["host-a-public0-tor1", "host-a-public0-tor2"],
  "table": 3100,
  "rule_priority": 10000,
  "sources": ["10.60.1.11/32"]
}
```

Each declared context needs one ready attachment per approved TOR. A missing,
wrong-domain or duplicate-TOR binding cannot activate. A dedicated context imports
routes only through its declared paths and exports its VIP only on those sessions.
A second context can therefore reach the same TORs using different VFs without
becoming a fallback path for the first context.

Omitting `attachment_ids`, or setting it to `null`, retains the existing shared
sessions model. An explicit empty list authorizes no paths; it cannot compile an
active context. Internally it can appear in a restrictive accepted-state floor
so removed path permissions cannot return after a failed replacement.

A changed path binding is a new grant. Restrictions commit the intersection of
previous and newly allowed paths first, withdraw affected origins, and do not
restore revoked pipes as the rollback target for a failed new topology. Host
source rules and unreachable guards remain provisioned by their owner.

## Disable NUMA preference explicitly

Use `numa_node: null` for a context whose two paths are equally acceptable:

```json
{
  "id": "any-numa",
  "machine_id": "example-host-a",
  "numa_node": null,
  "attachment_ids": ["host-a-general-tor1", "host-a-general-tor2"],
  "table": 3300,
  "rule_priority": 12000,
  "sources": ["192.168.50.11/32"]
}
```

Both paths receive equal BIRD preference and export preference marker `0`.
Either selected path is healthy rather than a NUMA fallback. This means there is
no NUMA preference; it does not enable ECMP, promise equal traffic distribution,
or move a physical VF between NUMA nodes. Strict Ceph contexts still require a
verified local attachment and reject unknown NUMA placement.

## Scope of the implementation

The new fields select context paths and preference within the existing
source-bound, allowlisted routing model. They do not make all ordinary host
traffic use those tables automatically. In particular, the current compiler still
rejects default routes and protected management imports. General API replies and
source-unspecified outbound connections need a separately reviewed default,
source-selection and management-isolation policy before production migration.

The DPU observer's physical-LLDP/OVS-bond mapping and the DPU's own routed Geneve
underlay are separate integrations. No live forwarding was changed by these
compiler changes. See [network ownership](design.md).

## Executed local verification

At this implementation checkpoint:

- 220 unit tests pass, including generated neutral-locality and path-reassignment
  floor tests. Existing shared-pair cases remain covered.
- 12 native BIRD checks pass, including ten-session parsing at 50 and 300ms.
- 20 isolated Linux namespace tests pass. The ten-path cases exchange real
  unnumbered BGP/BFD and packets, exercise silent loss, dedicated-pair failure,
  unaffected other contexts, neutral-path failover and management leak traps.
- One local LXD systemd test passes, exercising installed startup/restart guards.
- `uv check` and `make lint` pass.

These results do not prove physical DPU/TOR offload, routed Geneve operation or
production service migration. No new charm artifact or live upgrade is implied.
