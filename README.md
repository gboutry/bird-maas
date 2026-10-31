# MAAS BIRD

Three **principal machine charms**, with one shared Python package and typed relation
contracts. No charm is subordinate. This is an initial implementation, not a validated
production deployment or a 60-rack scale claim.

- **maas-inventory** holds the MAAS credential and publishes normalized, allowlisted
  machine/rack/routing identity records. Its client makes GET requests only, requires
  verified HTTPS, rejects redirects and never exports raw machine responses.
- **dpu-attachments** observes existing card identity, devlink host functions, explicit
  OVS trunk membership and LLDP chassis identity. It never changes forwarding, VF
  configuration, OVS or TORs. Deploy on each existing DPU machine.
- **bird** matches its hardware UUID to inventory and matches generic device identity
  records to locally discovered functions. It manages BIRD routing domains on an
  existing host, with IPv6 link-local BGP transport, IPv4 extended next hops, and BFD.
  It starts disabled. It does not create interfaces or VIP addresses. Optional NUMA
  contexts manage narrowly scoped source-policy rules and fail-closed guards.

## Repository

```
charms/{maas-inventory,dpu-attachments,bird}/  # independent charm projects and locks
shared/src/maas_bird/                        # models, interface APIs and workload logic
docs/schemas/                               # generated JSON schemas
examples/                                   # illustrative two-rack policy and DPU intent
tests/unit/                                 # Ops state transitions and shared logic
tests/native/                               # actual BIRD CLI and localhost BGP tests
tests/namespaces/                           # isolated unnumbered BFD and policy forwarding
tests/integration/                          # isolated Juju deployment smoke test
```

The shared package is private to this repository (`maas-bird-common`), not an already
registered/published Charmlibs interface. Cross-charm serialization and validation live
in `interfaces.py`/`models.py`; the routing renderer has no DPU implementation logic.
The current discovery backends support devlink device serials and permanent NIC MACs.
The DPU observer specifically supports explicit controller/PF/VF mappings on an OVS
trunk with directly observed LLDP. Complex bridge/bond reachability is not inferred.

## Development

```bash
uv sync --locked --python 3.12
uv check
make lint
make test
```

All three charms are based on Charmcraft's native machine template and use its `uv`
plugin. Each charm references `../../shared` as a normal, non-editable build dependency.
Per-charm lockfiles must be refreshed when shared dependencies change.

### Experimental Charmcraft monorepo builds

Requires a Charmcraft build supporting the experimental monorepo feature (documented
in 4.5). The environment variable is required **as well as a supporting binary**:

```bash
cd charms/bird
CHARMCRAFT_EXPERIMENTAL_MONOREPO=1 charmcraft pack --use-lxd --platform amd64
```

Each part uses `source: ../..` and `source-subdir: charms/<name>`. The enclosing Git
repository supplies the build root. The build includes the real sibling shared wheel;
there is no `sys.path` injection, copied vendored package, or mock packaging fallback.

Pack all projects with `uv run python scripts/pack.py`. If the installed snap lacks
this feature, `uv run python scripts/pack.py --development-charmcraft` uses a pinned
project-local Charmcraft source revision without changing the host snap installation. It selects a supporting Charmcraft
channel inside the disposable builders too (`CRAFT_SNAP_CHANNEL`, default
`latest/candidate`); an older inner builder can otherwise omit the charm entrypoint.
Arm64 is declared too: build it on an appropriate native builder with `--platform arm64`.

See [Charmcraft's monorepo guide](https://canonical.com/juju/docs/charmcraft/4/howto/pack-a-charm-in-a-monorepo/).

### Native BIRD checks

Use an installed BIRD 2 binary, or extract the Ubuntu bird2 package without installing
it on the host. Supply any extracted library directory through `LD_LIBRARY_PATH`:

```bash
BIRD_BINARY=/absolute/path/to/usr/sbin/bird make native
```

These tests parse both BFD profiles and start two temporary localhost BGP processes on
unprivileged ports. They check actual extended-next-hop negotiation, route exchange
and peer-shutdown withdrawal. They install **no kernel routes** and do not prove
unnumbered dataplane, BFD convergence, CPU overhead or hardware offload.

The deployment smoke test requires an explicitly selected disposable Juju controller:

```bash
uv run pytest tests/integration
```

It uses pytest-jubilant's temporary model and leaves routing disabled. It must never
be pointed at existing production hosts. Real TOR/DPU acceptance remains a separate lab test. The local namespace suite below
exercises synthetic TORs and actual kernel forwarding without accessing live infrastructure.

## Deployment outline

Place principal units on **existing machines**, not new guests or LXD containers. DPU attachment configuration is shared application intent: each unit selects records
matching its own observed serial. One DPU application can therefore have a unit per DPU.
The configuration list must include all intended cards; it is not a per-unit Juju config.

```bash
juju deploy ./charms/maas-inventory/maas-inventory_amd64.charm
juju deploy ./charms/bird/bird_amd64.charm --to <existing-host-machine>
juju deploy ./charms/dpu-attachments/dpu-attachments_arm64.charm dpu-a --to <dpu-a-machine>
juju deploy ./charms/dpu-attachments/dpu-attachments_arm64.charm dpu-b --to <dpu-b-machine>
juju integrate maas-inventory:inventory bird:inventory
juju integrate dpu-a:attachments bird:attachments
juju integrate dpu-b:attachments bird:attachments
# Integrate an authorized workload implementing route-advertisements as well.
```

Use cross-model offers when the DPU applications are in another model. Grant the
MAAS secret only to maas-inventory and session-password secrets only to bird. The
BGP secret contains attachment IDs as keys (e.g. `path-a`, `path-b`) and their passwords.
Set config from `examples/` after replacing illustrative identities. Our observed
lab MAAS HTTP endpoint is intentionally rejected: configure verified HTTPS first.

The host network owner must provision isolated point-to-point adjacencies, IPv6
link-local addresses, MTU, local VIPs on `lo`, and firewall policy. In legacy mode the
network owner supplies policy rules; NUMA mode owns only its explicit public selectors. TOR ASNs, peers, exact route filters, authentication
and BFD settings must be configured by their owner. This project does not configure
TORs, change replication or management, or claim that routes are hardware offloaded.
Only locally present, authorized IPv4 /32 service addresses are advertised.

After checking the input records and provisioning those prerequisites, explicitly
set `enabled=true`. A fresh enabled unit may install Ubuntu's `bird2` package, stop
its newly installed vendor service, and operate the dedicated `maas-bird.service`.
An existing active vendor BIRD service or another unit's ownership marker blocks
activation. There is one owner per host. NUMA mode uses multiple tables within that owner, with
one BGP session per adjacency. Decommission before changing owned tables or selectors.

## Safety and acceptance

Candidate configurations pass the actual BIRD parser. Updates use native `configure
... timeout 60`, verify all expected BGP/IPv4 channels, the neighbor's extended-next-hop
capability and BFD Up, then confirm. Failed acceptance undoes the candidate and retains
the previous config file. Initial activation has no previous owned service to restore;
it stops on failed acceptance. This does not provide coordinated TOR rollback.

MAAS/discovery outages retain last known data; explicit prefix withdrawal produces a
new config with no such export. Removal of a previously used relation/unit stops the
owned BIRD service rather than retaining authorization from a removed producer. Record
matching is not authentication: only integrate trusted producer applications. Shared
relation bags expose their records to all related units. Secrets never belong in them.

For contract fields, lifecycle, and scalability see [docs/design.md](docs/design.md).

## NUMA-aware public routing

Use [examples/numa-contexts.yaml](examples/numa-contexts.yaml) for two VIPs bound to
host NUMA nodes 0 and 1. Workload advertisements add a `context` field matching the
configured ID. Contexts can select `machine_id` so one application can contain host
specific mappings. Missing host mappings or unknown host PF NUMA locality block activation.

Each context uses its local adjacency first and the remote adjacency after BFD failure.
Only the specified VIP source **and** public destination prefixes select its table.
Management and replication prefixes must be declared in `protected-prefixes` and may
not overlap public imports. Traffic without an explicit VIP source is outside this policy;
the charm does not infer the NUMA node of a process or choose its source address.

Permanent unreachable defaults prevent fallback into the host main table when both
paths disappear. Disabling BIRD retains these guards and source rules; decommissioning
removes only acknowledged, journaled objects. Table/selector changes require decommissioning.
See [docs/design.md](docs/design.md) for rollback and ownership boundaries. This controls
host egress, not TOR ingress policy or hardware offload.

Run the real namespace tests using an installed or privately extracted BIRD 2 binary:

```bash
BIRD_BINARY=/absolute/path/to/usr/sbin/bird make namespaces
```

The runner requires `ip`, `nft`, `unshare`, and enabled unprivileged user namespaces.
It checks that user, mount and network namespaces are new **before** any mutation,
mounts a private `/run`, and creates synthetic hosts/TORs there. Tests cover 50/300 ms
BFD profiles, actual UDP egress, silent failures with carrier up, failback, both-path
failure, protected traffic, policy collisions, interrupted operations and rollback.
These are correctness tests, not CPU overhead or convergence benchmarks.

## Multiple routing domains

`routing-domains` enables separate Ceph public, Ceph replication, and other domains
within the same host-level BIRD owner. Each domain declares its own imports, export
allowlist, verified attachment domain, and per-host NUMA contexts. Domain IDs match the
existing `domain` field on attachment and advertisement relations; the workload also
supplies its context ID. No new charm or subordinate is required.

See [the complete example](examples/multiple-domains/bird.yaml), the accompanying
inventory/attachment/advertisement JSON documents, and
[the configuration schema](docs/schemas/routing-domains-config.json). The example has
public contexts in tables 3100/3101 and replication contexts in 3200/3201, with 50 ms
and 300 ms BFD respectively. These are illustrative addresses and timers, not live PC8
configuration or measured convergence targets.

Leave legacy `routing-contexts` empty when using `routing-domains`. Legacy single-domain
options continue to work when `routing-domains=[]`; the new mode uses domain-specific
import/export lists. Global BGP/BFD timers provide defaults, with optional domain-level
overrides. `protected-prefixes` lists management and other **unmanaged** networks;
managed replication prefixes belong in their replication domain instead.

Public and replication use distinct pre-provisioned interfaces/VFs or VLAN interfaces.
Their kernel tables, source VIPs and policy priority ranges must be globally unique;
managed destination prefixes and export allowlists must not overlap across domains.
No VRF or interface is created. This first multi-domain mode expects every unit to have
valid per-host contexts for every configured domain, and the same inventory-approved
rack TOR set in each domain. It does not support overlapping tenant address spaces.

Domains have separate BGP tables and filters. Source rules select their own context
for every managed destination, so a foreign-domain destination hits the context's
unreachable guard instead of falling through to the main table. Other sources and
unmanaged destinations are outside this routing policy. The host's higher-priority
local-address handling still applies; routing domains are not firewall or process
security boundaries.

One native BIRD candidate and one ownership journal cover the host. Confirmation
requires all domains' sessions and local routes. Timed rollback restores the previous
whole generation. Domain/table/selector reassignment requires decommissioning; foreign
objects and uncertain acknowledgements block automatic cleanup. A shared daemon or
trusted-relation removal affects all domains, while BFD path failures remain independent.

## Property-based testing

`make test` includes [Hypothesis](https://hypothesis.readthedocs.io/en/latest/)
properties and a journal lifecycle state machine. Run that subset with statistics:

```bash
uv run --locked pytest tests/unit/test_properties.py --hypothesis-show-statistics
```

The generated tests use fresh input factories per example, preserve canonical relation
semantics, vary domain layouts and resource collisions, and inject failures into every
installation position of the two-domain journal model. Real namespace tests remain the
forwarding and BFD boundary; the model is not a substitute for Linux or physical TORs.
Module-level imports are enforced with Ruff `PLC0415`.
