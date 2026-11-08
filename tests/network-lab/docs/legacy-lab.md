# Legacy fixture operations

The legacy helpers use project `maas-bird-network-lab`, a nested fabric VM,
manual SSH Juju allocation and separate runtime manifests. They run comparison
fixtures. The Terraform/Juju lab uses `maas-bird-network-lab-tf`; use the
[Terraform/Juju pipeline](../terraform/live/lab/README.md) for normal
deployment. Do not combine the projects' state, addresses, controller contexts
or cleanup.

The bootstrap entry point plans the legacy infrastructure, MAAS and manual
Juju controller. Review the resources and prerequisites before applying:

```sh
tests/network-lab/scripts/bootstrap.sh
tests/network-lab/scripts/bootstrap.sh --apply
```

`--phase infrastructure`, `--phase maas` and `--phase juju` select bootstrap
phases. Prepare host LXD, nested builder support and LLDP forwarding
permission for the legacy Linux carriers first. This profile uses manual Juju
allocation.

The legacy deployment CLI prints its phases without applying:

```sh
tests/network-lab/scripts/deploy.sh
```

`--apply` performs the selected legacy operation and requires existing
infrastructure, packed charms that match the source, private manifests, host
LLDP permission and verified lab machine identities. `--phase` selects a
phase; `--refresh-charms` refreshes applications; a routine Terraform
convergence check does not use it. Inspect `--help` and the helpers before
changing an existing lab.

Legacy test entry points are:

```sh
tests/network-lab/scripts/test.sh --suite model
tests/network-lab/scripts/test.sh --suite baseline
tests/network-lab/scripts/test.sh --suite process
tests/network-lab/scripts/test.sh --suite faults
```

Process/fault suites restart services, change links or reboot the nested
fabric. Run them serially with resource monitoring and restoration controls.
Keep run output in private, ignored runtime storage.

[Legacy Juju integration](../juju/bird/README.md),
[Containerlab fabric](../containerlab/README.md),
[provider transport](../containerlab/PROVIDER.md) and
[namespace gateways](../containerlab/GATEWAYS.md) describe which components
they configure. The Terraform/Juju pipeline does not use Docker or a nested
Containerlab fabric.

Preview legacy cleanup before applying it:

```sh
tests/network-lab/scripts/cleanup.sh
tests/network-lab/scripts/cleanup.sh --apply
```

Keep private state, credentials and required backups first. Cleanup must
verify resource identities. Use a separate migration or cleanup procedure for
the Terraform/Juju `maas-bird-network-lab-tf` project.
