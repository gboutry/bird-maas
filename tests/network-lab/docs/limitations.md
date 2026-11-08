# Known limitations

Use the lab to test routed networking software. An Active application,
successful plan or passing packet sample can still leave failures elsewhere.
The results do not establish production readiness.

| Area | Limitation |
| --- | --- |
| Cloud-init | Early MAAS reporting can leave workers in degraded completion with recoverable warnings. The strict cloud-init test still fails when networking works. Keep that failure visible. |
| Host-edge BFD | A path can withdraw, then have its BGP routes readmitted while BFD remains non-Up. The core's strict-mode results do not prove the same behavior at the host edge. |
| Attachment restoration | LLDP/attachment revalidation can exceed the 30-second limit for restoring both paths. Observer polling is separate from BGP/BFD convergence. |
| Geneve distribution | Geneve traffic can remain concentrated on one general link despite two kernel next hops and varied outer UDP source ports. Direct TCP can spread across both links while tunnel traffic stays on one. |
| Provider/gateway recovery | Losing only the provider VLAN does not trigger the required gateway takeover, even though whole-parent failover works. After a chassis binding returns, check NAT forwarding and stateful continuity separately. |
| Management | Stopping either access switch tests a separate software failure. Both paths still share the management upstream, endpoints and physical host. Apply each test's packet-loss requirements. |
| Hardware | VLAN children are not SR-IOV VFs. Cumulus/ASIC behavior, DPU firmware/offload, NUMA locality, physical cabling and line rate require real hardware. |
| Control-plane availability | A single MAAS/OVN-central VM is not a replicated production control plane. |

The lab does not renew certificates automatically. OVN certificates last 30
days; renew them or recreate the lab before they expire. The power frontend
assumes a combined region/rack controller. Controller and database exports
need a restoration test even after their readability checks pass.

Stage 70 can restart networkd/LLDP when adopting files, even if their contents
already match. Destroying only stage 65 removes its managed files without
restoring an older running switch configuration. Run the whole DAG at its
matching source revision, and keep private state and backups.

Moving from the older shared carrier to dedicated access switches requires a
reviewed teardown at the old source revision, followed by fresh creation.
Adding access containers before reducing controller allocations can exceed the
CPU cap. Do not mix generations or copy their state.

Resource caps, temperature samples and software failover measurements do not
define a physical performance SLA. Record creation, baseline, fault injection
and restoration results separately; [validation](validation.md) describes
their required checks.
