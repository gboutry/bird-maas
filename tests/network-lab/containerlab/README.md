# Legacy nested FRR fabric

This fabric runs in the [legacy project](../docs/legacy-lab.md),
`maas-bird-network-lab`. The native pipeline uses LXD system containers on the
host and needs neither Docker nor a nested fabric VM.

The VM contains four ToRs and two spines. Its routed /31 core links use explicit
loopback /32 filters, BFD and ECMP; host trunks carry VLANs 3400 through 3404.
Provider EVPN and namespace gateways have separate configurations. The fixture
pins router images and Containerlab downloads. All nested nodes share the VM's
resource cap and kernel, so their failures are only logically separate.
Unreachable `10.205.0.0/16` guards keep unresolved destinations off Docker's
management network.

From the repository root, compile the configuration before installing it on a
fresh fabric VM belonging to this lab:

```sh
uv run --locked python tests/network-lab/scripts/bootstrap_fabric.py
uv run --locked python tests/network-lab/scripts/bootstrap_fabric.py --apply
```

The helper checks VM ownership, resource caps, carrier LLDP permission and FRR
configuration. It refuses to overwrite an existing fabric.
`network-lab-fabric.service` brings up the installed topology after Docker and
networking start. Restarting the containers alone does not recreate data links.

For host routing, render into private runtime storage, validate, then apply:

```sh
uv run --locked python tests/network-lab/scripts/render_host_fabric.py --output tests/network-lab/runtime/compiled-host-fabric
uv run --locked python tests/network-lab/scripts/apply_host_fabric.py --directory tests/network-lab/runtime/compiled-host-fabric
uv run --locked python tests/network-lab/scripts/apply_host_fabric.py --directory tests/network-lab/runtime/compiled-host-fabric --apply
```

A configuration can parse successfully and still fail to forward packets.
Keep the previous private configuration and check what rollback would change
before maintenance. Stop provider and gateway services in their documented
order before replacing router namespaces. The helper refuses a plain-host
configuration while those services are installed.

For the fixed legacy profile, host root can enable LLDP on its four Linux
carriers while retaining the other mask bits:

```sh
sudo sh -c '
for b in nlab-probe-pf0 nlab-probe-pf1 nlab-r2-pf0 nlab-r2-pf1; do
    p=/sys/class/net/$b/bridge/group_fwd_mask
    v=$(cat "$p")
    printf "%s\n" "$((v | 16384))" > "$p"
done'
```

These Linux bridges need the LLDP bit. Recheck it after recreating a bridge,
and change only the listed legacy bridges. The OVS sudo policy does not apply to these Linux bridges; likewise,
the sysfs commands above do not apply to the native profile. Keep worker LLDP
receive-only and ToR transmission limited to the intended fast ports.

Run `containerlab destroy --topo` inside the fabric VM to remove the nested
nodes. MAAS machines and Terraform infrastructure remain until you remove them
separately. See [provider transport](PROVIDER.md), [gateways](GATEWAYS.md) and
[validation](../docs/validation.md) for their setup and checks.
