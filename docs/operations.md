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
   using explicit Juju placements. Applications may span racks: MAAS determines
   rack authority independently for each native unit UUID. A bound UUID/rack
   move revokes only that unit; review placement before reintegrating it.
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
5. Grant application-scoped BGP secrets containing session-ID to password entries. Integrate
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

The unit also compares each Up session's effective BFD transmit interval with
that routing domain's requested interval. For example,
`BFD TX 300ms, requested 100ms` puts the unit in Blocked status, even when BGP
and BFD remain Up. Check the peer's timer configuration and whether its BFD
session needs renegotiation. Missing or malformed timer observations are
reported as unavailable with Waiting status, allowing a later reconciliation
to retry the observation. Diagnostic logs include each affected interface and
its requested/observed interval. These checks do not stop usable routing sessions.

BIRD's effective transmit interval includes the peer's receive requirement;
a mismatch does not by itself prove that the local configuration failed to
apply. The BIRD session summary cannot independently verify the local receive
interval or multiplier, because its timeout also depends on the peer's timers.
Check native state at both endpoints before attributing a mismatch or claiming
the requested failure-detection timing is active.

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
and verify the persistent host policy, then deploy the unit-scoped model disabled.
Old options (`routing-domain`, `kernel-table`, `import-prefixes`,
`export-allowlist`, `routing-contexts`) are no longer supported. Preserve secrets
and allowlisted evidence through an operator-reviewed migration; do not auto-adopt
foreign routing tables. Unit placement explicitly authorizes takeover of the
host's system `bird.service` once routing inputs and static policy validate.
An existing parallel `maas-bird.service` or `/etc/maas-bird` layout blocks the new
service until the previous version has decommissioned it; the new unit never
starts a second routing daemon alongside it.

The charm uses the system `bird.service`, not a parallel daemon. Its private
accepted-state bundle and original systemd definitions live in
`/var/lib/maas-bird/`. BIRD reads a root-owned configuration materialized in
`/run/maas-bird-config/` only after the startup gate passes. Python code remains
in the packed charm environment; the service calls that environment's interpreter
with `-m maas_bird.external_policy`. No application Python is installed under
`/etc`. The host's original `/etc/bird` configuration is preserved.

On the first takeover, the original local unit and drop-ins, enablement and active
state are saved before the old daemon stops. Failure before the managed baseline
is acknowledged restores that service. After takeover succeeds, later routing
changes retain native timed rollback; they never restore the pre-charm exports.
Transient systemd overrides and vendor drop-ins require review before takeover.
Charm refresh rewrites the startup entry point if its interpreter path changes,
without stopping an unchanged accepted routing generation.

To remove the managed service: set `enabled=false`, then run `decommission confirm=true`.
The operation stops this unit's system BIRD daemon and deletes accepted routing
authority. It persistently masks `bird.service` and archives the private original
service backup under `/var/backups/maas-bird/` for operator recovery. Removing
the active ownership directory permits a later BIRD unit to take over normally.
Static VIPs/rules/guards, DPU forwarding and TOR configuration remain with their
owners. The mask prevents an old vendor configuration from advertising again after
removal or reboot. Restoring that backup and unmasking the service is an explicit
operator recovery operation.

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

For 60 racks of 40 hosts, one inventory app can address every BIRD unit
independently. One BIRD application and one DPU-observer application may span
those racks; separate apps remain an option for policy, upgrade or secret
isolation. Unit counts depend on hosts and cards. Each inventory response holds
one host and its approved peers, with a 48 KiB field bound. Application-databag
updates can still fan out to all consumer units; controller capacity is not
established by content tests.

A BIRD application per rack is also supported when smaller application groups
are useful: integrate each application with the same `maas-inventory:inventory`
endpoint and place its units on the intended hosts. Each relation carries only
responses for its own requesting units, not every machine known to MAAS. Do not
add a rack selector to either BIRD or the DPU observer: MAAS remains authoritative
for each unit's hardware UUID. This is an optional grouping, not a requirement;
units in one application can still belong to different racks.

Use the host/network source of truth to generate and review allocations. Start
with one host, then expand with measured hook latency, controller load and
recovery behavior. The 60-rack wire test demonstrates bounded fields, not Juju
capacity, event latency or convergence at that scale.
