# Legacy manual Juju integration

This directory supports [legacy fixtures](../../docs/legacy-lab.md) in project
`maas-bird-network-lab`. The [native pipeline](../../terraform/live/lab/README.md)
uses the MAAS provider and does not need these manual SSH steps.

The legacy controller uses Juju 3.6.29 manual bootstrap on its existing `juju`
VM. Workers get agents through Juju's supported SSH allocation after MAAS
installs them. This does not exercise the MAAS provider. Machine and unit IDs
can change after deployment. Find the application's single current unit, check its endpoint
and compare the guest's DMI UUID with MAAS and the LXD VM. Check that the LXD
VM belongs to the lab; matching a unit name alone is not enough.

After reinstalling a worker, retrieve its SSH host key through the checked LXD
connection, then allocate or reuse its manual agent:

```sh
uv run --locked python tests/network-lab/scripts/trust_guest.py --name probe-r2a
uv run --locked python tests/network-lab/scripts/rejoin_juju.py --name probe-r2a --replace-stale
```

Use `--agents-only` to allocate before applications exist. The rejoin helper
refuses workers with unrelated applications. Move a colocated inventory
publisher before redeploying its host. Keep credentials and the dedicated
known-hosts file private. SSH uses an explicit identity, strict host-key
checking and no connection sharing, without changing global SSH configuration.

Bind role recipes to the current LXD/MAAS/SSH UUIDs and permanent MACs:

```sh
uv run --locked python tests/network-lab/scripts/prepare_policy.py
uv run --locked python tests/network-lab/scripts/activate_roles.py
```

Activation changes live configuration. It checks deployed machines and role
interfaces, keeps existing passwords, stages observer/routing configuration,
validates FRR and enables BIRD. It refuses installed provider/gateway services
before compiling a plain host fabric. A failed operation stops the helper,
but it does not automatically roll back Juju configuration. Read the private
runtime receipt before resuming. `--members` selects a subset of workers;
test the full profile separately.

Put the controller's dedicated SSH wrapper ahead of Juju's packaged executables
and invoke `/snap/juju/current/bin/juju` directly in that environment. Take
controller addresses and keys from the legacy fixture's outputs rather than
the native profile. Do not force cloud registration past the manual-cloud
checks.

You can set the model's polling interval to bound attachment observation
refresh:

```sh
juju model-config -m network-lab:network-lab update-status-hook-interval=1m
```

An observer can start before LLDP arrives. It must keep admission not-ready
until it sees the expected chassis and port. This polling interval is separate
from BGP/BFD convergence and cannot guarantee lossless startup.

Keep nested Charmcraft/LXD setup inside the legacy fabric VM.
`build-lxd.yaml` and `builder-firewall.sh` are for that VM, not the LXD host.
Apply builder caps and egress rules there; never apply its preseed, Docker
firewall or storage/profile configuration to the host. For native packing,
use the repository Make target from a clean source tree.
