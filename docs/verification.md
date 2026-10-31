# Verification boundary

The implementation has been checked with:

- Ruff lint and format checks through the root Makefile.
- The actual `uv check` command, with no diagnostic suppressions or path exclusions.
- 127 unit and Ops state-transition tests covering contracts, identity joins, input
  authorization, read-only discovery, lifecycle removal and owned runtime guards.
- Five native BIRD 2.14 checks: generated 50 ms, 300 ms and multi-domain configurations
  parse; real
  localhost IPv4-over-IPv6 BGP exchanges/withdraws a route; native configuration timeout
  restores the previous generation and explicit confirmation retains a candidate.
- Fourteen disposable Linux namespace integration tests using actual BIRD 2.14, IPv6
  link-local BGP/BFD and kernel forwarding. Both 50 ms and 300 ms profiles exercise
  opposite preferred egress for two VIPs, silent faults with carrier up, BFD-triggered
  remote egress, failback, actual UDP delivery, and both-path failure. Tests assert
  failed route lookup plus exact unreachable-default guards (protocol 196, metric
  42760), and retain management/replication forwarding. Other cases check partial
  installation rollback, foreign collisions/replacements, lost acknowledgements,
  immutable policy, subsequent foreign-rule drift, initial runtime failure, and native timed candidate rollback.
- Experimental monorepo packing on amd64 using a supporting outer Charmcraft and
  Charmcraft 4.5 in the managed LXD builders. Archive verification checks dispatch,
  principal metadata, config and byte-for-byte equality of charm/shared Python sources.
- A prior isolated local Juju 3.6 deployment smoke test of the three packaged principal
  charms and their inventory/attachment integrations, with routing disabled. This
  checks the initial implementation’s real hooks and packaged imports. It was not
  rerun for the new NUMA policy feature; it does not validate that feature.

No PC8 host, DPU, TOR, MAAS configuration, replication path or management path was
changed. No snap, charm or source was published, and no remote branch was pushed.

## Not established

- Physical host-to-TOR/DPU routing, production convergence or forwarding performance.
  Namespace tests exercise synthetic TORs; they are not failover latency benchmarks.
- Driver/controller topologies beyond the adapters explicitly described in design.md.
- Hardware offload, Ceph load behavior, production rollback or reboot persistence.
- Arm64 packing/execution (declared as a target; requires a native supporting builder).
- Juju relation scalability at 60 racks. A synthetic 3000-host schema parse is not a
  controller scale test; the global inventory relation still fans out.

The deployment remains opt-in (`enabled=false` by default). Pre-provisioning interfaces,
addresses and TOR peers remains the network owner's responsibility. Legacy policy rules
remain externally managed; opt-in NUMA policy rules/guards are now owned by the charm.

## Reproduce the NUMA checks

```bash
make lint
make test
BIRD_BINARY=/absolute/path/to/usr/sbin/bird make native
BIRD_BINARY=/absolute/path/to/usr/sbin/bird make namespaces
```

For an extracted package, also set `LD_LIBRARY_PATH` to its library directory. Namespace
checks use the real `ip`, `nft`, `unshare`, BIRD and birdc commands. They require new user,
mount and network namespaces and refuse to run mutating checks without that isolation.
Kernel JSON parsing covers numeric route types and split address/prefix-length fields
observed from the executing iproute2. Earlier failed attempts were retained locally;
assertions were not relaxed to mask ownership/rollback bugs. The approved stderr-text
replacement instead checks the exact kernel guard and a real failed UDP send.

These checks do not establish physical NUMA locality, zero intersocket traffic,
hardware offload, CPU overhead, host reboot restoration, or automatic coordinated TOR
rollback. A live N5/TOR rollout and physical acceptance remain separate, unperformed work.

## Multi-domain extension

The public/replication extension has passed all 14 namespace cases and all five
native BIRD checks. The namespace suite observes public BFD at 0.050/0.150 seconds
and replication at 0.300/0.900 seconds, actual per-domain packet forwarding, rejection
of foreign-domain BGP routes and exports, cross-domain source-policy blocking,
independent domain failures/recovery, protected traffic, whole-install rollback and
unconfirmed multi-domain candidate rollback. These are synthetic correctness checks,
not physical TOR failover or CPU-overhead measurements.

The unit/state suite passes all 127 cases. Relation round trips compare complete
canonical databags and explicitly preserve public/replication domain and context
bindings; collection order is not part of the relation contract. The concrete examples
are validated against their generated schemas and shared Pydantic models.

The relation wire contracts retain their existing fields and compatibility. The new
`routing-domains-config.json` schema describes strict operator configuration; concrete
provider examples are in `examples/multiple-domains/`. No N5, DPU or TOR change has been
made. Physical NUMA locality, VRF/firewall isolation, hardware offload, reboot behavior,
Juju scale and live Ceph behavior remain outside this validation boundary.

## Property-based tests and import hygiene

Hypothesis is a development-only dependency, locked in the root `uv.lock`. It does not
change the shared runtime requirements or the individual charm lockfiles. Artifact
verification explicitly rejects packed Hypothesis modules or distribution metadata.

Six generated/stateful test cases cover:

- Canonical relation round trips, arbitrary member ordering, duplicate members,
  additive fields, and the distinction between unknown and explicitly empty snapshots.
- Generated domain tables, priority ranges, source bindings, destination sets and NUMA
  mappings, including input-order invariance and rejection of cross-domain collisions.
- Failures before each acknowledged installation operation, preserving foreign model
  objects and restoring the original state before a successful retry.
- Lost acknowledgements at each operation, refusing both adoption and automatic removal.
- Stateful sequences of installation, reconciliation, failed installation, retry and
  removal, checking ownership and foreign-object preservation after every transition.

Journal tests use real temporary files and fsyncs against an in-memory kernel-command
model. They do not invoke `ip`, emulate a complete Linux kernel, or replace namespace
acceptance. Their filesystem timing is not a performance assertion. Existing example
and namespace assertions remain in place.

All Python imports are now at module scope. Ruff `PLC0415` enforces this through the
native `make lint` target. Fixture builders are shared with generated tests through
`tests/factories.py`, without pytest-fixture reuse across Hypothesis examples or
suppressed fixture health checks.

## Static type validation

`uv check` validates all charms, shared code and tests. Single-domain resolution returns
`Plan` directly; the multi-domain dispatcher composes those plans without a recursive
union. Juju string options are validated without coercing non-string values. Optional
relation members and dynamically loaded test modules are checked before access.
Existing assertions remain intact, with additional presence/type assertions where needed.
