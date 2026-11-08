# Isolated FRR comparison fixture

The candidate Dockerfile pins the official FRRouting 10.7.1 image by digest.
The [Noble baseline](../baselines/noble-8.4/Dockerfile) keeps the FRR 8.4.4
alternative. Use these pinned files rather than a moving image tag.

The candidate has the legacy seed fabric's routed links, ASNs, loopbacks and
prefix filters, but separate Docker management and network namespaces. Both
run inside the legacy fabric VM's resource cap. This fixture has no host or
provider links. It is separate from the native pipeline's Ubuntu FRR package
installation.

Run the routing checks from the repository root:

```sh
taskset -c 0,1 uv run --locked pytest -q tests/network-lab/tests/routing --frr-lab=maas-bird-fabric-candidate
```

The comparison checks kernel ECMP recovery as well as BGP/BFD. Healthy sessions
do not prove that the kernel has restored its next hops. Keep the image's
userspace, the VM kernel, protocol state and packet delivery separate when
interpreting results. Neither image tells you how Cumulus or physical switches will converge.
The [legacy guide](../../docs/legacy-lab.md) covers project ownership and
lifecycle.
