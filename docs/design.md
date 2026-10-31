# Unit-scoped routing design

## Ownership and scale

One MAAS adapter holds the MAAS credential. Its leader performs GET-only HTTPS
requests with certificate verification and no redirects. This client does not
make an otherwise privileged MAAS token read-only at the server. Use the least
privileged service identity MAAS supports. No API keys, full machine objects or
unrelated MAAS metadata enter relations.

A BIRD application can span racks. Each unit publishes its native DMI UUID; the
adapter joins that UUID independently to MAAS and an approved `rack-<id>` tag.
Neither the BIRD application nor the DPU application selects a rack. A bad or
unknown unit cannot choose the rack of its neighbors. A separate fabric
controller and DPU MAAS credentials are unnecessary.

A deployment can use one BIRD app for hosts and one DPU observer app across its
racks. Separate BIRD apps remain useful when operators need different application
policy, upgrades, or secret trust boundaries. Application configuration and Juju
secret grants are shared across that application's units. BIRD may also run on
DPUs for their own routed underlay; attachment observation remains a separate
role and does not provision OVS.

The provider writes one application-databag field `unit-<id>` per actual consumer
unit. Its JSON value contains only that unit's own host and approved TOR policy.
Every value is bounded to 48 KiB; there is no single all-rack host array to exceed
that bound. Canonical unchanged responses cause no writes. Initial bindings and
new unit identities require a fresh MAAS fetch; ordinary relation events reuse a
bounded cache for already bound units. Application relation changes can still
fan out across units. The 60-rack wire representation is tested, but Juju event
latency and controller capacity at that scale have not been measured.

## Relations

The shared library is a private project interface, not a registered Charmhub
interface. Schemas under `docs/schemas/` are exported from the runtime models.
The inventory provider application databag contains addressed JSON documents;
other interfaces use one canonical JSON value per named field.

| Endpoint | Interface | Writer | Fields and meaning |
|---|---|---|---|
| `inventory` | `machine-inventory` | Each BIRD unit | `system_uuid`: observed local DMI UUID |
| `inventory` | `machine-inventory` | Inventory leader | `unit-<id>`: unit name, bound UUID, MAAS rack, authorization state and one host record |
| `attachments` | `host-attachments` | Each DPU observer unit | `cards`: native card identity and attachment observations; no rack header |
| `advertisements` | `route-advertisements` | Workload owner unit | `rack`, `machine_id`, `domain`, `advertisements`: readiness for that host/domain |

For example, a response for `bird/12` is stored under `unit-12` and includes
`unit_name: bird/12`. The consumer validates both the addressed key and its full
unit name, then requires the returned UUID to match its own native identity.
It never selects another unit's response. Each host record contains MAAS machine
ID, UUID, rack, ASN, router ID and approved TOR identities.

A unit response is `ready`, `pending`, `conflict` or `revoked`. Incomplete identity
or an API outage is pending and cannot grant new authority. A successful response
proving unknown, duplicate or invalid identity withdraws only affected units.
A bound unit moving rack or changing UUID leaves a durable revoked tombstone in
its own response. Reintegrate deliberately after reviewing such a move. This
binding survives leader replacement. During outages, current policy removals
still restrict previously published hosts and peers. Another rack remains
independent.

There is one current relation contract. Application-wide inventory responses and
consumer-selected rack headers are not read or used as fallback authority.

DPU observers publish the same local card observations regardless of which
racks share the application. BIRD selects observations using its native card
identity, then checks attachments against its own MAAS-authorized TORs. Missing
card observations and departed producers revoke only their previously selected
attachments. DPU observations have no rack scope header.

An attachment identifies a card using `{scheme,value}` and has `id`, `domain`,
`function`, `observed_function`, `ovs_port`, `vlan`, `chassis_id`,
`peer_link_local`, `ready`.
`function` is the canonical **host** controller/PF/VF tuple. `observed_function`
is the native DPU tuple. They may differ; provisioning must declare the mapping.
The host matches card identity plus exact function/VLAN, and verifies local NUMA
and scoped link-local adjacency. A retained attachment ID does not authorize a
changed function, VLAN, peer, or TOR chassis.

For a BIRD unit co-located with OVS, an attachment may instead identify a local
internal port through `ovs_port`, including its native row UUIDs and kernel
ifindex. Physical and logical selectors are mutually exclusive. Local discovery
is opt-in; changed OVS identities cannot inherit accepted authority by name.
See [DPU LACP and OVS attachments](ovs-attachments.md) for configuration and
verification boundaries.

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
machine, NUMA cell, dedicated table, reserved source-rule priority range and /32 VIPs. Each
context reserves one consecutive priority per source, beginning at
`rule_priority`; the range must not collide with another context or exceed
32000.
VIP allocations cannot overlap between hosts or domains. Management prefixes
cannot be imported or used as managed source bindings.

The provisioning owner installs one source-wide rule per VIP, selecting that
context's table for every destination, followed by an exact unreachable-default
guard (protocol 196, metric 42760). BIRD installs only approved imports into those
tables. If both fabric paths fail, the guard prevents falling through to `main`.
Ordinary management sources retain their existing policy. The kernel local table
remains first, so local delivery is intentionally not intercepted by this design.

By default, contexts share the domain's sessions. A context can instead name
`attachment_ids` to select a dedicated pair, allowing separate VF pairs for its
two NUMA VIPs. See [dedicated paths](dedicated-paths.md). A numeric `numa_node`
requires a local attachment and prefers it over the remote TOR; an explicit
`numa_node: null` selects no NUMA preference. BIRD exports a large community `(host-ASN, context-table, preference)`
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
