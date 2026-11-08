# Failure domains

A fast parent carries five dependent logical children. When testing parent
loss, make all five children unusable before checking the surviving paths.
VLAN roles represent logical VF placement, not hardware SR-IOV. Discovery
reads PCI ancestry and permanent MAC addresses. Leave VF indices, DPU serials
and NUMA placement unset when the hardware does not report them.

| Domain | Independent operation to validate | Shared dependency |
| --- | --- | --- |
| Fast parent | Lose the whole parent and all dependent children | Hypervisor and carrier backend |
| Role path | Lose one access, replication or general child | Parent and ToR |
| Management access | Stop either access switch with guest carrier still up | Management upstream, endpoints and host |
| Routed fabric | Withdraw the intended spine/ToR adjacency or data path | Host kernel and switching service |
| Provider | Lose provider connectivity while Geneve remains reachable | Gateway and routed transport |
| OVN gateway | Check selected gateway, bindings and actual NAT packets | OVN central and provider L2 |

Check source-bound packets and the allowed next hops, then check restoration.
A surviving route or Active application can coexist with broken forwarding. If
a role loses all its paths, its unreachable routes must stop traffic from
falling back through management. Test OVN gateway selection and NAT as well as
EVPN MAC distribution, and send packets after a redirect binding returns.

All software switches share the hypervisor kernel and all VMs share one host.
Two management access switches do not remove the shared upstream or controller
failure domain. Separate gateway VMs do not reproduce DPU hardware or physical
rack failures. See [limitations](../docs/limitations.md) for known recovery
problems and [validation](../docs/validation.md) before running disruptive
tests.
