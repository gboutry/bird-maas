# Testing the lab

Use a checkout that matches the deployed lab and its private state. Test the
operation you need to check: parsing configuration, inspecting a schema,
hashing an archive or planning cannot tell you whether packets forward, a
fresh deployment completes or a failed service recovers. Hardware needs its
own tests.

## Baseline

Run these baseline checks from the repository root. They cover inventory,
routing, gateways, OVN/NAT, Juju applications, secrets and workers:

```sh
uv run --locked pytest -q   tests/network-lab/tests/lxd/test_native_inventory.py   tests/network-lab/tests/lxd/test_native_frr_routing.py   tests/network-lab/tests/lxd/test_native_gateways.py   tests/network-lab/tests/lxd/test_native_ovn.py   tests/network-lab/tests/lxd/test_native_ovn_nat.py   tests/network-lab/tests/terraform/test_native_applications.py   tests/network-lab/tests/terraform/test_native_secrets.py   tests/network-lab/tests/hosts/test_native_workers.py   -k 'not failure and not failover and not restart and not reconfiguration and not frr_stop_and_start and not spine_and_evpn_peer_recovery and not nat_stability and not nat_with_spine and not general_vf and not geneve_blackhole'
```

The selection excludes disruptive and long-running cases. Packet fixtures
create temporary endpoints or namespaces in the lab and clean them up. OVN
tests include TLS/RBAC rejection checks. Keep strict cloud-init failures
visible even when the networking tests pass.

## Disruptive checks

Before injecting faults, check that the lab is disposable, verify resource
ownership and have restoration controls and resource monitoring ready. The
dedicated management suite stops real access containers while guest carriers
stay up:

```sh
uv run --locked pytest -q tests/network-lab/tests/hosts/test_native_management_access.py
```

Check traffic on the surviving path and check restoration afterward. Compare
instance identities, permanent MACs, bond/IP state, VLAN parents, allowed
kernel next hops and BGP/BFD. Streams must keep progressing during the fault
and after restoration; record loss, sequence holes, duplicates and reordering.
Keep a missed restoration deadline as a failure even if the surviving path
works.

Other suites cover role/parent loss, BFD, router/service restart, provider
faults, worker reboot, encapsulation and NAT. Read each test's resource
checks, fault and cleanup steps before running it. Use baseline checks for
routine readiness instead of running every fault file. Seed-only FRR adjacency
expectations apply before stage 65; use the routing-context suite after
routing is installed.

## Deployment, teardown and artifacts

Start a fresh-creation test with the lab's state and infrastructure verified
empty. Run the full DAG without mocked dependency outputs, then rerun Make to
check convergence. Teardown must reverse the matching DAG and remove guest
files before releasing machines. Preserve the controller-owned management
subnet as configured. Check that the lab resources are gone and unrelated
resources remain.

Verify packed charm metadata and application source against the checkout, then
verify the actual upload files and installed agent sources. Building or
inspecting a ZIP does not tell you which files the target uses. Provider
tests, checksums and schema checks also need a real apply and destroy. Test
restoration from controller/database exports separately.

Keep run output, JUnit, resource samples, state and credentials in private,
ignored runtime storage. Record source/tool/package identities and distinguish
host usage, allocated limits, QEMU RSS, physical filesystem use and ZFS
accounting. Sampling leaves gaps between observations.

Physical testing needs the chosen Cumulus release/ASIC and DPU/NIC firmware,
real SR-IOV assignment, NUMA/IRQ/CPU placement, MTU and tunnel-option budgets,
flow distribution, provider-only failure and stateful NAT continuity. Use
physical hardware to test these.
