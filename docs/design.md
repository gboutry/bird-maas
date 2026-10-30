# Contracts and ownership

The proposed interfaces are `machine-inventory`, `host-attachments`, and
`route-advertisements`. These names have not been registered as public standards.
Their library lives in `maas_bird.interfaces`; runtime implementation is independently
testable and contains no Juju operations. Both endpoints import the same models.

## Relation data

Every top-level model field defaults to `None`. Empty bags and unknown additive fields
parse; incompleteness is handled separately from parsing. Collections serialize as
stable arrays of objects, are deduplicated on reception, and are not keyed by arbitrary
rack names or hardware IDs. Top-level values are JSON strings in Juju databags.

| Interface | Writer | Model / field |
|---|---|---|
| machine-inventory | provider application leader | Inventory.hosts |
| machine-inventory | consumer unit | InventoryRequest.system_uuid |
| host-attachments | provider unit | Attachments.cards |
| route-advertisements | trusted workload unit | Advertisements.advertisements |

The schemas in `schemas/` are generated directly from the code with `make schemas`.
Consumers tolerate unknown fields and must not change the meaning or type of published
fields. Old/new test vectors must pass before library compatibility changes. No
mandatory version envelope or version-suffixed interface name is assumed.

An absent advertisement field means unknown, not withdrawn. An explicit empty array
is the producer's complete empty snapshot. Every producer must supply a complete
snapshot; records for other hosts are ignored after validation. Duplicate matching
inventory or DPU producers are rejected. Matching host/card identity is not a permission
grant. Producers are trusted through explicit Juju integration, with an operator export
allowlist as a further constraint.

## Host and DPU matching

MAAS hardware_uuid joins the host's DMI product UUID. It must be unique among allocated
hosts. MAAS machine tags `rack-a`/`rack-b` select explicit profiles `a`/`b`; the MAAS
rack-controller identity is not interpreted as a physical rack. ASN and router ID
allocations are explicit, with duplicate allocations rejected.

On N5, read-only observations established these equalities between host PFs and DPU
ARM-side devlink devices (this is evidence for those cards, not fleet-wide attestation):

| Host PF | Device serial | Board serial |
|---|---|---|
| 0000:41:00.0 | d0d69bc89789ef118000b8e9243735f6 | 53FV4AC00K6 |
| 0000:c1:00.0 | 3ad6c4c29789ef118000b8e9243735d0 | 53FV4AC00K5 |

Generic identity scheme/value pairs join attachment records to local discovery. The
host walks sysfs physfn/virtfn links to find the actual VF number; PCI BDF and Linux
interface names are not global identities. The initial host discovery adapter assumes
controller 0 and conventional PF PCI function numbers. Other controller topologies
need a new validated adapter, not relaxed matching.

The DPU provider checks configured card serial, devlink coordinates, explicit OVS
trunk membership, common bridge membership, and LLDP chassis identity. It does not
prove end-to-end reachability or hardware offload. It cannot infer isolation through
arbitrary bonds/bridges; only use approved, pre-provisioned isolated adjacencies. A host
can therefore match a DPU without the DPU having MAAS access or knowing the host UUID.

## Local routing

The initial operator manages one routing domain per host, with exact IPv4 import
prefixes, authorized /32 service exports, one link-local session per approved TOR,
and one exclusive kernel table. It does not implement NUMA-specific source-policy
selection, ECMP, automatic peer discovery, TOR configuration or interface provisioning.
BFD timers are application-wide and measured negotiated behavior is authoritative.
50ms x3 is nominal detection around150ms under symmetric negotiation, not50ms service
recovery. Authentication secrets are required for the BGP sessions; BFD authentication
and network admission controls remain explicit deployment requirements.

## 60 racks

One inventory application can serve all racks without one MAAS credential per host.
Its leader currently performs one batched MAAS machine-list read per reconciliation
and publishes compact allocated records only when content changes. Data volume is O(H)
for H allocated hosts. Delivery to all H BIRD units can approach O(H^2) aggregate
inventory traffic during a complete refresh; there is no hidden per-unit targeting.
DPU unit relations have their own fan-out as well.

Do not claim 60-rack support without measuring actual host counts, payload sizes,
relation events, controller queueing, full refresh and recovery. Partition applications
and relation inventories if those measurements require it. Selective relation
publication is not implemented in this first version. DPU units select their own
serials from shared application intent; changes to that list still fan out. Runtime BGP/BFD failure detection remains local and independent of this
control-plane scale limit.

## Verification levels

1. Unit tests: interface evolution, identity joins, filtering, guards and rollback paths.
2. Ops state-transition tests: actual charm event handlers and unit/app relation writes.
3. Native tests: BIRD2.14 parses configs; localhost daemons exchange and withdraw a route.
4. Pack: experimental Charmcraft monorepo resolves the shared sibling dependency.
5. Isolated Juju smoke: packaged hooks and relation behavior with routing disabled.
6. Still required: real unnumbered two-TOR BFD faults, kernel path selection, supported
   DPU topologies, CPU load and hardware offload. No production readiness claim follows
   from the previous levels alone.
