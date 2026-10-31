# Deploy a first MAAS BIRD pilot

Start with one host and two top-of-rack (TOR) switches. Keep the pilot separate
from production service addresses and retain out-of-band access. The steps below
explain what each owner must prepare before the charm can route traffic.
They do not provision a fabric or supply a missing workload integration. This
walkthrough uses the default workload mode. For node-owned transport addresses,
see the explicit [node-address mode](node-addresses.md) and its native checks.

The example uses a two-NUMA-node host and two routing domains named
`ceph-public` and `ceph-replication`. These are policy names; another workload
can use the same model if it supplies the required readiness relation.

## Before you start

| Requirement | What to establish |
|---|---|
| Hosts | Ubuntu 24.04, usable DMI hardware UUIDs, and Linux interfaces with discoverable card/function identity and known NUMA nodes. BIRD 2.14 is the current target. |
| Juju | Controller and agents 3.6 or later. Each host and DPU OS must already be enrolled as a Juju machine; these charms do not enlist or provision them. |
| MAAS | A reachable HTTPS API with trusted certificates; matching `system_id` and hardware UUID records; exactly one approved `rack-<id>` tag per participating machine. |
| Credentials | An appropriately privileged MAAS service identity and a Juju secret granted only to inventory. GET-only charm behavior does not make an administrator token read-only. |
| Switches | The unnumbered BGP/BFD capabilities and explicit routing policy described below. |
| Host networking owner | Persistent VLANs/interfaces, IPv6 link-local addresses, /32 service addresses, source rules and guard routes; workload boot ordering behind that policy. |
| Attachment provider | Trusted observations joining each local NIC/function/VLAN to an approved TOR. The supplied DPU observer needs compatible devlink, OVS and LLDP state. |
| Workload publisher (default mode) | A real `route-advertisements` provider that checks readiness and withdraws addresses when they are no longer ready. No Ceph adapter is shipped here. |

**An ordinary NIC does not require a DPU.** Host discovery supports permanent
port MACs as an identity fallback. However, this repository does not supply an
ordinary-NIC attachment publisher. To use that path, first provide and validate
an implementation of the [attachment relation](design.md#relations). Do not
pretend a DPU observer is a generic NIC provider or mark an attachment ready
without evidence.

Current host discovery also requires conventional PCI function coordinates and
exactly one usable non-tentative IPv6 link-local address on each selected
interface. Numeric NUMA contexts require known placement; an explicit neutral
context can omit the locality preference. See [dedicated paths](dedicated-paths.md).
Unknown placement (including a reported `-1`) blocks a strict NUMA context. The supplied observer matches MAC-type LLDP chassis IDs.
These constraints need checking on a different NIC, DPU or switch implementation.

If either publisher or persistent host policy is missing, you can deploy the
charms disabled and inspect inventory. That is a useful integration check, not
a working routing deployment.

## 1. Allocate the pilot

The files under [`examples/getting-started/`](../examples/getting-started/)
use the following illustrative values. Replace them through your normal IPAM,
ASN allocation and host provisioning process before deploying.

| Item | Example |
|---|---|
| Rack / MAAS tag | `a` / `rack-a` |
| Host MAAS system ID | `example-host-a` |
| Host ASN / router ID | `4200000101` / `10.255.1.11` |
| TOR1 / TOR2 ASN | `65010` / `65011` |
| TOR chassis identities | `02:00:00:01:00:01` / `02:00:00:01:00:02` |
| Management prefix to protect | `192.0.2.0/24` |
| Public sources, NUMA0 / NUMA1 | `10.60.1.11/32` / `10.60.1.12/32` |
| Replication sources, NUMA0 / NUMA1 | `10.61.1.11/32` / `10.61.1.12/32` |
| Public tables / rule priorities | `3100, 3101` / `10000, 10100` |
| Replication tables / rule priorities | `3200, 3201` / `10200, 10300` |
| Public VLANs to TOR1 / TOR2 | `3500` / `3501` |
| Replication VLANs to TOR1 / TOR2 | `3510` / `3511` |

The tag is `rack-` followed by the rack ID: ID `a` uses tag `rack-a`.
MAAS availability zones are a separate concept: do not change them to encode
physical racks.

`machine_id` in both `host-identities` and routing contexts is the MAAS
`system_id`, not a hostname or a Juju machine number. Replace `example-host-a`
with that identifier from your inventory.

The example imports only its own public and replication /24s. A multi-rack
service needs an explicit reviewed list of the other reachable domain prefixes.
Do not install a default route or management prefix just to make a check pass.
Host ASNs and router IDs must be unique across the deployment; /32 source
allocations must not overlap between hosts or domains.

## 2. Prepare host networking

The host network owner must persist these objects using its existing network
management system:

- One approved interface and usable IPv6 link-local adjacency per TOR and domain.
  Keep interface names within Linux's 15-character limit.
- Every declared service /32 configured on the host's `lo` interface. The
  charm waits until each context source is present there as a /32.
- One source-wide rule per /32, selecting its dedicated context table for every
  destination, at its reserved priority. The rule protocol is `196`. With multiple sources in a context, reserve
  consecutive priorities starting at `rule_priority`, one per source, ending no
  higher than 32000.
- An unreachable default in each context table, protocol `196`, metric `42760`.
  It remains when BGP routes disappear, preventing fallback to `main`.
- Startup ordering that prevents the workload using these sources before the
  static policy is ready. BIRD's own startup checks do not gate a separate service.

Check the actual state with native Linux tools before configuring BIRD:

```sh
ip -j -d address show
ip -j -d rule show
ip -j route show table 3100
ip -j route show table 3101
ip -j route show table 3200
ip -j route show table 3201
ip route get 10.60.1.20 from 10.60.1.11
```

Before BGP supplies an allowed route, a nonlocal lookup from a managed source
should fail through its guard rather than use management. After activation,
verify its intended path. Also verify that ordinary management sources retain
their previous routing. These checks must use your assigned addresses/tables.

BIRD checks this policy and supplies dynamic routes; it does not create or repair
static interfaces, addresses, rules or guards. Do not use a global route/rule
flush as a preparation or rollback step.

## Switch requirements

Ask the fabric owner to prepare and persist the following on both TORs:

- Unnumbered eBGP over IPv6 link-local with IPv4 extended-next-hop. Link-local
  addresses are scoped to interfaces; the same `fe80::1` can be used on separate
  links only when both ends and scopes are configured consistently.
- One adjacency per approved TOR per host/domain. The current resolver expects
  one attachment for every approved peer and rejects duplicate TOR bindings
  within a domain. The two-TOR example has four sessions on the host.
- Matching host/TOR ASNs, BGP authentication and BFD. Allow BGP TCP179,
  single-hop BFD UDP3784 and IPv6 neighbor discovery on the intended links.
- BGP keepalive/hold timers matching the example's 3s/9s, and graceful restart
  disabled for these service adjacencies.
- Exact incoming /32 allowlists and prefix limits. Each example session can
  export two VIPs for its domain, so its TOR-side maximum-prefix is two.
  Each TOR must send the exact prefixes listed in the BIRD domain's
  `import_prefixes`; more-specific routes are filtered unless separately listed.
  For this example, send its /24, not a stream of other hosts' /32s. BIRD
  limits accepted imports to the number of configured prefixes and disables a
  session if that limit is exceeded.
- Real reachability behind any advertised aggregate, appropriate route
  propagation between TORs/racks, and isolation between public, replication and
  management. The host charm does not configure fabric route exchange or VRFs.
- An ingress preference policy for BIRD's large communities:
  `(host-ASN, context-table, 1)` means the preferred local-NUMA attachment;
  `(host-ASN, context-table, 0)` means backup. For example, set TOR local
  preference 200/100 after validating the announcing host and prefix.

Start at 300ms TX/RX with multiplier three. A later 50ms TX/RX profile must match
both sides and be measured under idle and workload conditions. Its nominal
150ms detection interval is not a guarantee of 50ms packet recovery.

Global timer options use hyphens, such as `bfd-min-tx-ms`. A domain can override
them inside `routing-domains` using `bfd_min_tx_ms`, `bfd_min_rx_ms`,
`bfd_multiplier`, `bgp_hold_time_seconds` and `bgp_keepalive_time_seconds`.
For example, set `bfd_min_tx_ms: 50` and `bfd_min_rx_ms: 50` on replication while
public inherits 300ms. BFD intervals must be 50–60000ms; the multiplier is 1–255;
BGP hold time must be at least three keepalive intervals. Always coordinate an
override with that domain's TOR sessions.

The DPU forwarding owner must independently prepare the correct representor,
VLAN and uplink path. The supplied observer checks existing state only. If kernel
LLDP names and OVS uplink names differ, confirm that the implementation can
observe the actual layout; do not substitute an unverified name to force Ready.

BGP configuration syntax and persistence belong to the switch vendor and its
configuration owner. Do not copy FRR commands onto another implementation or
mix unmanaged FRR edits with a system that regenerates them. Keep the exact
vendor configuration and rollback plan in the deployment operator workspace.

## 3. Deploy disabled and deliver inventory

Build the artifacts using the [README instructions](../README.md#build-the-charms).
The commands below assume the resulting amd64 charms and existing Juju machines
`0` (inventory), `1` (host), `2` and `3` (DPU cards). Replace placements, model and
architecture. None of these example machine numbers is discovered by the charm.
Do not place a second routing owner onto a host already managed by another one.

Edit `inventory.yaml` before running the deploy command below.
Create a **private** file containing the MAAS secret's `api-key` field. Its value
is the existing `consumer-key:token-key:token-secret` credential. Do not put the
value in shell arguments, version control, relations or a shared config file.

```sh
juju add-secret maas-inventory-access --file /PRIVATE/PATH/maas-secret.yaml
# Substitute the returned secret ID for secret:REPLACE-MAAS in inventory.yaml.
juju deploy ./charms/maas-inventory/maas-inventory_amd64.charm maas-inventory \
  --to 0 --config ./examples/getting-started/inventory.yaml
juju grant-secret secret:REPLACE-MAAS maas-inventory

juju deploy ./charms/bird/bird_amd64.charm bird-rack-a \
  --to 1 --config ./examples/getting-started/bird.yaml
juju integrate maas-inventory:inventory bird-rack-a:inventory
```

If MAAS uses a private CA, create a private YAML file with a `ca-cert` field
containing its public PEM certificate, then grant/configure its secret:

```sh
juju add-secret maas-ca --file /PRIVATE/PATH/maas-ca.yaml
juju grant-secret secret:REPLACE-CA maas-inventory
juju config maas-inventory maas-ca-certificate=secret:REPLACE-CA
```

At this point inventory can publish the host, while BIRD remains disabled. Check
`juju status` and the inventory relation; do not interpret successful deployment
as established BGP. Use verified HTTPS and a trusted public/private CA. The
separate HTTP lab build described in the historical handoff is not this
mainline deployment path.

## 4. Integrate attachment and workload publishers

For a compatible DPU layout, select the artifact matching the card OS architecture
(`arm64` on the physical pilot cards). Replace every card serial, uplink name
and function mapping in `dpu.yaml` with observed values. Its `function` coordinates are the
DPU namespace; `host_function` explicitly identifies the host function. They
need not have the same controller number. Pick VF allocations with the host
owner; an example VF number is not a reservation against Nova or another user.

For the shipped DPU observer, both function tuples require explicit PF and VF
coordinates, and `host_function.controller` must be `0`. The host PF number is
the PCI function digit, not an assumed front-panel port index. The host must
observe the same devlink card serial as the DPU; use an existing host VLAN
netdev and explicit OVS representor trunk membership for the declared VLAN.
Choose lower-case, hyphen-separated attachment IDs usable as Juju secret keys,
such as `host-a-ceph-public-tor1`.

Each observer unit selects only config entries matching card serials it
observes locally; both cards can appear in the application-level config.

| DPU configuration key | Published relation field | Meaning |
|---|---|---|
| `serial` | `cards[].identity` | Observed devlink serial identifying the card on host and DPU. |
| `function` | `observed_function` | DPU-native controller/PF/VF coordinates. |
| `host_function` | `function` | Canonical host controller/PF/VF coordinates used for matching. |
| `vlan` | `vlan` | Explicit representor VLAN membership, matching the host VLAN device in this example. |
| `chassis_id` | `chassis_id` | Approved TOR identity checked against the observed LLDP neighbor. |
| `peer_link_local` | `peer_link_local` | TOR link-local address; scope comes from the matched host interface. |

```sh
# This example uses ARM card operating systems. Build their artifact first:
uv run python scripts/pack.py --development-charmcraft --clean --platform arm64 dpu-attachments
uv run python scripts/verify_artifacts.py --platform arm64 dpu-attachments
juju deploy ./charms/dpu-attachments/dpu-attachments_arm64.charm dpu-rack-a \
  --to 2 --config ./examples/getting-started/dpu.yaml
juju add-unit dpu-rack-a --to 3
juju integrate dpu-rack-a:attachments bird-rack-a:attachments
```

A provider can also live in a separate model using a private cross-model offer.
Limit offer access to the intended consumer; do not expose inventory credentials.
Applications may span racks. MAAS determines each BIRD unit's own rack from its
native hardware UUID and `rack-<id>` tag; neither consumer charm has a `rack`
option. DPU observers publish rack-neutral native card observations. BIRD joins
only its own matching cards to its own approved TOR policy.

Next, integrate your actual workload's `route-advertisements` provider with
`bird-rack-a:advertisements`. That provider must publish the host identity,
domain, /32 prefix, context and observed readiness. An empty complete snapshot
withdraws that producer's routes. See the [relation contract](design.md#relations)
and [exported schemas](schemas/route-advertisements-provider.json).

There is no deploy command here for a shipped workload adapter: one is not
included. Do not replace it with a hand-edited relation databag. Until a real
publisher is available and checked, leave routing disabled.

## 5. Review and enable the pilot

Fill a private copy of
[`examples/getting-started/bgp-secret.TEMPLATE.yaml`](../examples/getting-started/bgp-secret.TEMPLATE.yaml) with passwords matching the
TORs. Its keys are the exact attachment IDs, which become session IDs. Create a
Juju secret, grant it only to `bird-rack-a`, and set its returned ID:

```sh
juju add-secret bird-rack-a-auth --file /PRIVATE/PATH/bgp-secret.yaml
juju grant-secret secret:REPLACE-BGP bird-rack-a
juju config bird-rack-a bgp-credentials=secret:REPLACE-BGP
juju run bird-rack-a/0 plan
```

A ready advertisement outside the configured export allowlist is not exported.
Check the declared source/context and allowlist before debugging a missing TOR route.

Review the proposed host/domain bindings, interfaces, peer identities, source
contexts and allowed prefixes. Resolve all prerequisites first. Review the
bounded failure test and rollback with the host/fabric owners before proceeding.

```sh
# Only after the review and preparation above:
juju config bird-rack-a enabled=true
juju run bird-rack-a/0 reconcile
juju run bird-rack-a/0 inspect
```

Check action **task status and results**, not only the CLI exit code. `inspect`
shows the accepted plan; it does not prove packets are using it. Confirm actual
Established BGP, Up BFD, intended routes, source-context packet paths and
management/replication isolation on both sides. For hardware offload, collect
DPU/TOR hardware counters for the surviving path during each failure and recovery.

## If activation does not complete

| Observation | Next check |
|---|---|
| Inventory cannot publish | HTTPS/CA, secret grant and actual MAAS credential privilege; rack tag, system ID and hardware UUID. |
| BIRD reports routing disabled | Expected until explicitly enabled; inspect inputs and finish prerequisites first. |
| Attachment not ready | Card identity, exact controller/PF/VF mapping, VLAN membership, LLDP chassis and actual OVS uplink layout. |
| Waiting for advertisements | A real publisher is missing, unknown, incorrectly scoped or not ready; do not fabricate readiness. |
| Static policy mismatch | Compare the source-wide rules and exact guards with the proposed contexts; use the provisioning owner to fix them. |
| BGP/BFD does not establish | Both ends' interface scopes, link-local addresses, ASNs, secrets, timers and protocol access. |
| Advertisement rejected | Its prefix or context does not match a declared context source; correct the publisher or reviewed allocation, not the TOR filters. |
| Imported routes missing | TOR export policy, BIRD import allowlist, and actual domain reachability; do not broaden to default as a workaround. |

Do not paste secret contents or the root-private accepted bundle into a support
report. Use allowlisted plans and scoped native observations.

## Stop, recover or grow

To stop the pilot, set `enabled=false` and verify the owned daemon stops and its
advertisements withdraw. For intentional removal, run
`juju run bird-rack-a/0 decommission confirm=true`. The host rules/guards, DPU
forwarding and switch configuration remain with their respective owners. Keep
guards while workloads can still use the sources. See
[operations](operations.md#updating-and-rollback) for ownership and rollback rules.

After a successful pilot, add host-specific allocations and contexts, then add a
BIRD unit on each participating host. Add hosts in another rack to the same
application when they share its policy and lifecycle. DPU observers can likewise
share an application across racks. The [two-rack example](../examples/rack-scoped/bundle.yaml)
shows one BIRD application, one observer application and one inventory provider.
Use separate applications when policy, upgrades or secret trust boundaries differ.
Validate controller/event capacity and failure behavior at your actual scale
before calling it production-ready.
