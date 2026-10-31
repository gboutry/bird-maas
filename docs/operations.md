# Operations and migration

For a first deployment, follow [Getting started](getting-started.md). This
runbook covers operation of the resulting host policy and routing service.

## Admission

1. Allocate unique machine/ASN/router IDs in `maas-inventory`, with exactly one
   approved rack tag per MAAS machine. Grant the MAAS credential secret only to
   the inventory app. The `maas-credentials` option references a secret with an `api-key` field.
   Optional `maas-ca-certificate` references a secret with a `ca-cert` PEM field;
   TLS verification cannot be disabled.
2. Deploy BIRD on each existing host and DPU observers on each existing card,
   using rack-specific applications and explicit Juju placements. Integrate
   only matching rack applications. A rack relation scope is immutable; remove
   and reintegrate it for an intentional rack move.
3. Provision interfaces, IPv6 link-local addresses, loopback /32 VIPs, source-wide
   policy rules and exact unreachable guards persistently. Gate workload startup
   on those objects. Confirm source selection and NUMA attachment identities.
   BIRD expects protocol196 by default. For Netplan/networkd-owned static rules
   and guards, explicitly set `policy-protocol=4`. The chosen tag applies to both
   rule and guard checks; selectors, priorities, tables, catch-all scope and
   metric42760 remain exact. It is not permission to adopt other static routes.
4. Configure both TORs for unnumbered IPv4 extended-next-hop BGP, matching BFD,
   filters, limits and ingress preference policy. Independently verify DPU/TOR
   offload. Neither observer nor BIRD makes these physical changes.
5. Grant per-rack BGP secrets containing session-ID to password entries. Integrate
   workload readiness publishers. Review `plan`, then explicitly enable routing.
   Absence of the external workload/provisioning integrations is a release gate,
   not a reason to fabricate ready advertisements.

The example bundle is disabled and uses illustrative existing machine numbers.
It does not provision hosts, DPUs or switches and must not be applied unchanged.
Do not put raw credentials in YAML, relation data, actions or support archives.

## Checks

Use `juju run <bird-unit> inspect`, `plan`, and `reconcile`. Inspect reports the
accepted plan only. Plan does not apply it. Reconcile reports current unit status;
Active requires native BGP/BFD and the declared imported routes. A fallback path
is reported as degraded rather than fully local service.

For physical acceptance, collect scoped BIRD/BFD state, both directions' route
selection, packet loss/outage distributions, hardware flow/ASIC counters, and
per-process CPU under idle and Ceph load. Exercise each TOR/path, then both paths,
and recovery. Verify management and replication independently throughout. A
50 ms BFD interval with multiplier 3 is approximately 150 ms nominal detection;
it is not a 50 ms measured end-to-end failover guarantee.

## Updating and rollback

Ordinary grant/topology proposals use native timed rollback. Withdrawals have
no stale rollback target. A static table/source/priority/protocol migration must be
provisioned and reviewed explicitly; it is not performed by this charm. Resolve
reported drift before restarting. Do not manually restore an old private bundle:
it may reauthorize revoked exports or credentials.

For an upgrade from the prototype, disable and explicitly decommission its owned
service/policy first using that version's ownership checks. Independently install
and verify the persistent host policy, then deploy the rack-scoped model disabled.
Old options (`routing-domain`, `kernel-table`, `import-prefixes`,
`export-allowlist`, `routing-contexts`) are no longer supported. Preserve secrets
and allowlisted evidence through an operator-reviewed migration; do not auto-adopt
foreign BIRD files, tables or vendor services.

To remove the new service: set `enabled=false`, then run `decommission confirm=true`.
The operation stops only this unit's owned BIRD daemon and removes its own files.
Static VIPs/rules/guards, DPU forwarding and TOR configuration remain with their
owners. A vendor `bird.service` mask installed to prevent package-postinst startup
is intentionally retained; do not resurrect an unreviewed vendor daemon.

## Release gates

Ubuntu 24.04/BIRD 2.14 is the current target. Arm64 packaging/runtime acceptance,
real MAAS integration with restricted service credentials, workload boot gating,
60-rack controller scale, and physical TOR/DPU/offload and load acceptance must
have their own evidence before a production release. Local namespace packets,
LXD systemd and synthetic-HTTPS Juju tests exercise different boundaries and do
not replace those gates.

## Juju action results

Check each task's structured `status` as well as its results. On the deployed
Juju 3.6.29 CLI, `juju run` returned exit status zero even when the task status was
`failed`. An unavailable authoritative inventory therefore remains a failed
`plan` action, not a successful plan merely because the CLI exited zero.
`reconcile` also reports the workload status explicitly; a completed action does
not mean the unit is Active or that routing is enabled.

## Planning multiple racks

For 60 racks of 40 hosts, the intended topology is one inventory application,
60 BIRD applications and up to 60 DPU-observer applications. Each BIRD application
has 40 units. DPU unit counts depend on cards per host; workloads have their own
publisher units. Relation fan-out stays within a rack, but all units and events
still consume controller capacity. JSON relation fields have a 48 KiB limit;
measure actual rack inventory/attachment payload sizes before rollout.

Use the host/network source of truth to generate and review allocations. Start
with one host, then expand by rack with measured hook latency, controller load
and recovery behavior. Existing 60-by-40 content/property checks do not establish
controller capacity, event latency or convergence at that deployment scale.
