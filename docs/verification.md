# Verification boundary

The implementation has been checked with:

- Ruff lint and format checks through the root Makefile.
- 95 unit and Ops state-transition tests covering contracts, identity joins, input
  authorization, read-only discovery, lifecycle removal and owned runtime guards.
- Four native BIRD 2.14 checks: generated 50 ms and 300 ms configurations parse; real
  localhost IPv4-over-IPv6 BGP exchanges/withdraws a route; native configuration timeout
  restores the previous generation and explicit confirmation retains a candidate.
- Eleven disposable Linux namespace integration tests using actual BIRD 2.14, IPv6
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
