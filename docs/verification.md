# Verification boundary

The implementation has been checked with:

- Ruff lint and format checks through the root Makefile.
- 77 unit and Ops state-transition tests covering contracts, identity joins, input
  authorization, read-only discovery, lifecycle removal and owned runtime guards.
- Four native BIRD 2.14 checks: generated 50 ms and 300 ms configurations parse; real
  localhost IPv4-over-IPv6 BGP exchanges/withdraws a route; native configuration timeout
  restores the previous generation and explicit confirmation retains a candidate.
- Experimental monorepo packing on amd64 using a supporting outer Charmcraft and
  Charmcraft 4.5 in the managed LXD builders. Archive verification checks dispatch,
  principal metadata, config and byte-for-byte equality of charm/shared Python sources.
- An isolated local Juju 3.6 deployment smoke test of the three packaged principal
  charms and their inventory/attachment integrations, with routing disabled. This
  checks real hooks and packaged imports, not just mocked Ops events.

No PC8 host, DPU, TOR, MAAS configuration, replication path or management path was
changed. No snap, charm or source was published, and no remote branch was pushed.

## Not established

- Real host-to-TOR unnumbered routing, BFD fault convergence or forwarding performance.
- Driver/controller topologies beyond the adapters explicitly described in design.md.
- Hardware offload, Ceph load behavior, production rollback or reboot persistence.
- Arm64 packing/execution (declared as a target; requires a native supporting builder).
- Juju relation scalability at 60 racks. A synthetic 3000-host schema parse is not a
  controller scale test; the global inventory relation still fans out.

The deployment remains opt-in (`enabled=false` by default). Pre-provisioning interfaces,
addresses, policy rules and TOR peers remains the network owner's responsibility.
