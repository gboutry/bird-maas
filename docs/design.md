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

With `routing-contexts=[]` the existing single-table behavior is unchanged. Legacy
policy rules remain externally managed. NUMA mode is explicit and uses host PF sysfs
`numa_node` (following the VF's `physfn`), not a DPU ARM CPU node. An unknown node blocks
NUMA activation. Interfaces, addresses and TOR peers remain pre-provisioned.

### Context configuration and relations

`routing-contexts` is a JSON array with `id`, optional `machine_id`, `numa_node`, `table`,
`rule_priority`, and `sources` (authorized VIP /32s). `machine_id` refers to the MAAS
machine ID. A host selects its entries; a nonempty configuration with no host entry
blocks rather than silently selecting legacy mode. Omitting `machine_id` applies the
entry to every host, so fleet-specific addresses normally need explicit machine IDs.
The corresponding `route-advertisements` record adds the optional `context` field:

```json
{"machine_id":"abc123","domain":"storage-public","prefix":"10.60.11.11/32",
 "ready":true,"context":"numa0"}
```

This is additive to the existing relation schema; legacy mode accepts old producers.
NUMA mode requires the context ID on every matching advertisement. Reserved source
bindings remain even when an advertisement is withdrawn; only ready, locally present
VIPs are exported. The DPU relation does not acquire a host NUMA field: matching card
identity plus host-local PCI discovery supplies locality without making BIRD DPU-specific.

### Route selection

There is one BGP/BFD adjacency per approved attachment. Each BGP adjacency has its own
BIRD table, preserving the route from each TOR before a context makes its decision.
One-way pipes copy BGP routes into each context, preference 200 for the local NUMA
attachment and 100 for a remote attachment. Separate kernel protocols install the
context winners in dedicated kernel tables. Local service routes are exported through
both adjacencies; ingress preference remains a separate TOR policy.

For every configured source /32 and exact approved public import prefix, a rule selects
that context's table. In single-domain mode, rule priorities start at `rule_priority` and consume
`sources × imports` consecutive values. Context IDs, tables, sources and priority ranges
must be unique. Tables are >=1000, priorities 1000–32000. Public imports cannot be default
routes or overlapping prefixes. `protected-prefixes` is mandatory and must include the
operator's management/replication networks; any overlap with imports blocks activation.
This protects declared networks, not undisclosed network intent.

The process must bind/use the correct VIP source. This does not pin a process, migrate
memory, steer IRQs or prove that all packet handling stays within a NUMA node. No ECMP,
TOR provisioning or hardware offload verification is implemented.

### Ownership, rollback and failure behavior

Before activation, all context tables must be empty, priorities unused, and overlapping
foreign policy rules absent. Kernel policy uses protocol 196; each table has an
`unreachable default` at metric 42760. Rules match only source plus public destination,
so protected destinations retain their existing routing. A missing BGP route hits the
guard instead of falling through to the main-table management default. Guards are owned
by the charm, independently of BIRD, and survive daemon stop/crash. Disabling the charm
or losing a trusted relation stops BIRD but keeps policy fail-closed.

The root-owned journal records owner, exact layout, acknowledged operations and a
pending operation. It is fsynced before mutations. Initial failure removes only exact
acknowledged objects, after stopping the owned BIRD service. A present operation with
no recorded acknowledgement is ambiguous: reconciliation and removal block for operator
inspection rather than adopting or deleting it. Foreign replacements also block cleanup.
No rule/table flush is used. Decommissioning removes the owned service and acknowledged
policy objects; selectors, priorities and tables are immutable until decommissioning.

BIRD candidates pass the real parser and use native timed reconfiguration. Confirmation
requires all expected BGP sessions, IPv4 extended-next-hop negotiation, BFD Up and a
NUMA-local route for every public import in every context. Unconfirmed candidates roll
back to the previous BIRD generation without changing policy bindings. Autonomous BFD
failover can temporarily use remote paths; a subsequent reconciliation can report
waiting while degraded rather than confirming a new degraded candidate. This is not
coordinated rollback with TORs or a guarantee of atomic recovery across daemon/host crashes.

Legacy BFD timers are application-wide; multiple-domain overrides are described below.
Negotiated behavior is authoritative. 50 ms x3 is
nominal detection around 150 ms under symmetric negotiation, not 50 ms service recovery.
BGP authentication secrets remain mandatory; BFD authentication and network admission
controls remain deployment requirements.

## Multiple-domain ownership

The optional `routing-domains` JSON array is validated with `RoutingDomain` and
`ContextIntent`; its schema is `docs/schemas/routing-domains-config.json`. Operator
configuration rejects unknown fields so a misspelled timer cannot silently select a
default. Relation records retain their existing additive-field compatibility.

Each domain has an `id`, `import_prefixes`, `export_allowlist`, and `contexts` array.
Optional `bfd_min_tx_ms`, `bfd_min_rx_ms`, `bfd_multiplier`,
`bgp_hold_time_seconds`, and `bgp_keepalive_time_seconds` override application defaults.
The same local ASN/router ID and inventory-approved TOR set apply to each domain.
Context IDs are scoped to their domain; tables, source bindings, interface ownership,
session credential IDs and rule priority ranges are host-wide and cannot collide.

All domains resolve before any runtime mutation. Each matches only attachments and
advertisements bearing its domain ID. Unknown host NUMA locality, an absent host
context, missing readiness, or invalid domain intent prevents accepting a generation.
Explicitly withdrawing replication advertisements removes those exports without
withdrawing public exports. An incomplete snapshot retains the previous generation;
it is not an authoritative withdrawal.

The wire schemas already carry the required information, so no incompatible relation
change or new relation endpoint is introduced:

| Interface | Domain information | Scope |
|---|---|---|
| `machine-inventory` | Host UUID, MAAS ID, router ID/ASN and approved rack TORs | Shared host identity |
| `host-attachments` | `cards[].attachments[].domain`, unique session ID, function/VLAN and peer | One domain per approved adjacency |
| `route-advertisements` | `advertisements[].machine_id`, `domain`, `context`, `prefix`, `ready` | Authorized service source in one domain/context |

The files in `examples/multiple-domains/` show complete provider documents before
encoding. Each top-level relation field remains JSON-encoded in the Juju databag by
`maas_bird.interfaces`. MAAS inventory is not copied onto DPUs; they still publish
observed card identity and attachment data, while host discovery supplies NUMA locality.

### Forwarding and failure isolation

The renderer namespaces all domain-specific tables, direct protocols, pipes and kernel
protocols, with globally unique BGP protocol numbers. It never pipes routes between
domains. Exact import filters reject routes offered on the wrong domain session;
service exports are restricted to that domain's ready local VIPs. There is one Device
protocol and one BFD instance, with separate interface timer clauses per domain. Multiple
unrestricted BFD instances would compete for session requests; the shared instance
avoids that ambiguity. See the [BIRD BFD documentation](https://bird.nic.cz/doc/bird-2.18.html).

For each source VIP, policy rules cover the union of all managed destination prefixes,
but every rule selects that VIP's own context table. Only its domain's routes are
installed there. Foreign-domain destinations therefore hit the unreachable default.
Each source consumes `all managed destinations` priorities, including these cross-domain
guards; validation reserves the complete range. Unmanaged management destinations do
not match these rules. Route acceptance also rejects foreign unicast destinations in
a context table, rather than confirming a potentially leaking candidate.

This is policy separation for configured sources and forwarded destinations, not a
security boundary between local processes or host-local addresses. The kernel local
routing table is consulted first. Overlapping networks, VRF provisioning, firewall
isolation and source enforcement are outside this implementation.

### Transaction boundaries

The ownership journal includes domain IDs and context/table/source assignments. An
active layout cannot be silently renamed, removed, reassigned or expanded; explicitly
decommission before changing it. Initial multi-domain installation adds all guards
before rules, and a known failure rolls back all acknowledged operations across domains.
An ambiguous operation, foreign replacement or owner mismatch still blocks adoption
and deletion. Cleanup never flushes a table or removes foreign routes.

One BIRD candidate contains all domains. The parser and native timeout cover the whole
candidate; the charm confirms only after all expected BGP/BFD sessions and all context
routes satisfy acceptance. A failed candidate leaves the existing journal and previous
BIRD generation intact. A timer/preference change can be attempted without changing the
immutable kernel layout. A domain path failure can use its own remote NUMA backup while
other domains keep local paths. Host-wide daemon failure or removal of a trusted producer
is a shared failure domain by design, not an independently restartable per-domain service.

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
6. Namespace integration: actual unnumbered BGP/BFD, source routing, packet delivery,
   silent faults, fail-closed behavior and rollback on synthetic TORs.
7. Still required: physical TOR/DPU faults, supported DPU topologies, CPU load and
   hardware offload. No production readiness claim follows
   from the previous levels alone.
