# Legacy provider EVPN transport

The legacy fixture uses provider VLAN/VNI 3405 and route target `65000:3405`.
ToRs terminate VXLAN on their routed loopbacks. Provider ports and VXLAN join
only the unaddressed `br-provider`; host-role trunks stay separate. EVPN import
policy matches the provider route target and preserves advertised VTEP next
hops. VXLAN data-plane learning is disabled, so the tests can check EVPN MAC
installation separately.

The namespace probes check provider Ethernet transport. They cannot check DPU
behavior, OVN gateway selection or NAT redundancy.

From the repository root, render into private storage, validate, then apply:

```sh
uv run --locked python tests/network-lab/scripts/render_provider_fabric.py --output tests/network-lab/runtime/provider-evpn
uv run --locked python tests/network-lab/scripts/apply_host_fabric.py --directory tests/network-lab/runtime/provider-evpn
uv run --locked python tests/network-lab/scripts/apply_host_fabric.py --directory tests/network-lab/runtime/provider-evpn --apply
uv run --locked python tests/network-lab/scripts/deploy_provider.py --apply
uv run --locked pytest -q tests/network-lab/tests/provider/test_evpn_transport.py
```

Without `--apply`, the deployment helper checks identities and prerequisites.
It marks the interfaces and namespaces it creates and refuses conflicting
objects. Namespace markers include the VM boot identity: the helper can rebuild
objects after a reboot without taking over an unowned namespace in the same
boot.

Replacing Containerlab routers removes their namespaces. Stop the lab's
provider and gateway services first, then restart provider before gateways.
The fabric helper handles this ordering; direct Containerlab operations must
do the same. An Active oneshot service does not tell you whether its links
survived an uncoordinated replacement. See [legacy gateways](GATEWAYS.md) and
[validation guide](../docs/validation.md).
