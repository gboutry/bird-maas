# Legacy namespace gateways

`gateways.json` defines separate network namespaces and OVS/OVN processes
inside the capped legacy fabric VM. These gateways share a VM, so they cannot
reproduce separate VM failures or DPU hardware. The native pipeline uses
separate gateway VMs; keep this sequence out of that project.

Install `ovn-host` and `openvswitch-switch` alongside OVN central in the fabric
VM. Its global OVN controller and switch services must not compete with the
namespace instances. Complete the legacy role deployment, OVN setup and
provider fabric first, then run this sequence from the repository root:

```sh
uv run --locked python tests/network-lab/scripts/deploy_gateway_nodes.py
uv run --locked python tests/network-lab/scripts/render_gateway_fabric.py --output tests/network-lab/runtime/gateway-fabric
uv run --locked python tests/network-lab/scripts/apply_host_fabric.py --directory tests/network-lab/runtime/gateway-fabric --apply
uv run --locked python tests/network-lab/scripts/deploy_gateway_nodes.py --apply
uv run --locked python tests/network-lab/scripts/configure_gateway_imports.py
uv run --locked python tests/network-lab/scripts/configure_ovn_gateway.py
uv run --locked pytest -q tests/network-lab/tests/provider/test_ovn_gateways.py
uv run --locked pytest -s -q tests/network-lab/tests/provider/test_gateway_failover.py
```

Read each command's help and check its ownership requirements before
maintenance. Keep certificates, CA keys, generated manifests and credentials
private. Southbound TLS keeps chassis RBAC; northbound access stays local to
the fabric VM. `network-lab-gateways.service` starts after provider and OVN
central services. Before replacing routers, stop the gateways; restart them
only after provider links exist.

Gateway fault tests stop real processes and data links, then restore them in
cleanup. Check redirect bindings and full-MTU NAT packets separately. If packets
fail after a binding returns, that is still a failure even if forwarding
recovers later. Namespace separation alone says nothing about physical
provider-path HA or stateful continuity. See the
[known limitations](../docs/limitations.md).
