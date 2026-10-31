# Rack-scoped routing design

## Ownership and scale

One MAAS adapter holds the MAAS credential. Its leader performs GET-only HTTPS
requests with certificate verification and no redirects. That limits this
client's behavior; it does not make an otherwise privileged MAAS token read-only
at the server. Use the least privileged service identity MAAS actually supports.
No API keys, full machine objects or unrelated MAAS metadata enter relations.

Each BIRD application selects one immutable rack. The adapter publishes only
that rack's hosts and peers to that relation. Each BIRD unit selects its physical
DMI UUID, then selects matching card identities and host/domain workload headers
before validating payloads. A malformed unrelated producer cannot block a local
withdrawal. A separate fabric controller is not required.

With 60 racks of 40 hosts, the topology is one inventory app, 60 BIRD apps and up
to 60 DPU observer apps. BIRD/DPU/workload relation fan-out stays inside each rack.
A changed relation is serviced individually; a MAAS refresh can update all rack
slices, while canonical unchanged databags generate no writes. Relation-event
requests reuse a bounded positive/negative MAAS cache. Each JSON field is limited
to 48 KiB. Content/property checks are not evidence that a 2,400-unit Juju model
meets an event-latency or controller-capacity objective.

## Relations

Wire format is one canonical JSON value per named relation field. The private
shared library is not a registered public Charmhub interface. Schemas are
exported directly from the models under `docs/schemas/`; additive unknown fields
are ignored, while invalid known fields reject the snapshot.

| Endpoint | Writer | Fields and meaning |
|---|---|---|
| `machine-inventory`, consumer app | BIRD leader | `rack`: immutable rack request |
| `machine-inventory`, consumer unit | Each BIRD unit | `system_uuid`: observed local DMI UUID |
| `machine-inventory`, provider app | Inventory leader | `rack`, `hosts`: selected MAAS identities, ASNs, router IDs and approved TORs |
| `host-attachments`, provider unit | Each DPU unit | `rack`, `cards`: card identity and attachment observations |
| `route-advertisements`, provider unit | Workload owner | `rack`, `machine_id`, `domain`, `advertisements`: complete readiness snapshot for that host/domain |

A host record contains `machine_id`, `system_uuid`, `rack`, `local_asn`,
`router_id`, and peers with `id`, `chassis_id`, `asn`. The inventory app joins
explicit routing allocations with MAAS `system_id`, `hardware_uuid` and exactly
one `rack-<id>` tag. Missing or invalid host observations confer no new authority.
A complete successful host removal revokes it; a failed API request does not
synthesize an empty snapshot. During an outage, only restrictions of previously
published authority may be delivered.

An attachment identifies a card using `{scheme,value}` and has `id`, `domain`,
`function`, `observed_function`, `vlan`, `chassis_id`, `peer_link_local`, `ready`.
`function` is the canonical **host** controller/PF/VF tuple. `observed_function`
is the native DPU tuple. They may differ; provisioning must declare the mapping.
The host matches card identity plus exact function/VLAN, and verifies local NUMA
and scoped link-local adjacency. A retained attachment ID does not authorize a
changed function, VLAN, peer, or TOR chassis.

Workload records contain `machine_id`, `domain`, `prefix`, `context`, `ready`.
They may advertise only explicitly bound /32 sources inside the operator's
allowlist. An unavailable snapshot retains the last accepted producer observation;
a complete empty list or departure withdraws that producer's exports. Duplicate
local domain publishers quarantine the disputed domain, rather than blocking
valid withdrawals elsewhere. `inspect` exposes an allowlisted plan, not secret
contents or the private startup bundle.

Relations establish application-level trust through Juju. A self-reported unit
header, card serial or LLDP chassis is not cryptographic hardware attestation.
Only integrate trusted publisher applications; explicit operator allocations
remain the upper bound on their grants.

## Domains and NUMA policy

`routing-domains` is the only supported BIRD configuration model. Each domain has
approved imports/exports and host-bound contexts. Every context names a MAAS
machine, NUMA cell, dedicated table, reserved source-rule priority and /32 VIPs.
VIP allocations cannot overlap between hosts or domains. Management prefixes
cannot be imported or used as managed source bindings.

The provisioning owner installs one source-wide rule per VIP, selecting that
context's table for every destination, followed by an exact unreachable-default
guard (protocol 196, metric 42760). BIRD installs only approved imports into those
tables. If both fabric paths fail, the guard prevents falling through to `main`.
Ordinary management sources retain their existing policy. The kernel local table
remains first, so local delivery is intentionally not intercepted by this design.

Each context prefers the attachment on its NUMA cell and retains the other TOR
as backup. BIRD exports a large community `(host-ASN, context-table, preference)`
with preference 1 for the local attachment and 0 for backup. The TOR owner must
implement corresponding ingress policy. These communities are intent, not proof
of NUMA locality at a DPU or hardware forwarding after failover.

## Lifecycle and rollback

One root-private atomic bundle contains the accepted plan, configuration, policy
recipe, producer bindings, credential hashes and native generation token. It
contains BGP passwords in its configuration and must never be included in support
bundles. The root startup helper checks ownership, host UUID, configuration hash
and current static policy, then materializes a root-owned, bird-readable volatile
configuration. BIRD runs without root, with narrowly declared network capabilities.

First startup uses a zero-export baseline. New grants use BIRD's native 60-second
configuration timeout, require the intended generation and necessary sessions/
paths, and receive positive confirmation inside a shorter 35-second deadline.
A no-op confirmation is not acceptance. Agent death before confirmation leaves
the native daemon responsible for timed rollback.

Restrictive changes first persist an authorization floor and never keep a stale
undo target. Live acknowledgement failure stops the owned daemon; restart can
load only that floor. Invalid static-layout proposals preserve valid accepted
routing and require an explicit provisioning migration. Drift in accepted static
policy stops routing. Inventory/credential authorization removal invalidates
startup authority before stopping. Decommission removes only owned daemon files,
leaving provisioning-owned network objects intact.

Revocation is not instantaneous while the Juju agent is stopped or disconnected.
The durable floor prevents resurrection after it has been committed; it is not an
out-of-band liveness guarantee. Host provisioning must also gate workloads at
boot: starting Ceph before static guards exist is outside what BIRD's own prestart
checker can protect.
