# Synthetic rack fixture

This fixture exercises three hosts, six DPU cards, dedicated public and
replication paths, and a NUMA-neutral general path. Hardware identifiers are
synthetic and IPv4 addresses use documentation ranges. It is test input, not
a deployment allocation or observed hardware state.

The Netplan tests run only the generator in disposable roots. They never apply
these files. Preserve the compiler, policy and forwarding-ownership assertions
when changing the fixture.
