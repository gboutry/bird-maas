# MAAS LXD power-driver probe

This fixture enrolls a Terraform-created blank LXD VM into the lab's MAAS
controller. It is separate from normal [native deployment](../../live/lab/README.md).
Keep variable files, credentials and state private and outside Git.

The standard nginx frontend handles certificate and URL-path compatibility
between the MAAS LXD driver and LXD. It verifies upstream TLS and rewrites
`/1.0/virtual-machines` to `/1.0/instances`; it adds neither a custom power
protocol nor a Python power service. The driver needs an RSA client key,
and LXD trust must restrict that identity to the probe project.

The BMC endpoint uses the combined region/rack controller's own management IP.
Terraform checks that the address routes locally over `lo` and that frontend
port 944 is privileged. This setup requires a combined controller; it does not show that frontend TLS
is safe for a separate remote rack controller. The connection to
LXD uses a pinned server certificate/name and restricted client key.

The probe checks commissioning, hardware UUID, unauthenticated rejection,
other-project denial, incorrect trust-anchor rejection and restoration:

```sh
uv run --locked pytest -q --no-cov tests/network-lab/tests/lxd/test_maas_lxd_power.py
```

Probe certificates last 24 hours and renewal is not automated. Destroy stops
and disables the probe's nginx service and removes its configuration; the OS
package stays installed. The helper refuses an existing unowned nginx
installation. MAAS TLS on port 5443 is independent. Before using this for a
long-lived or remote-controller deployment, resolve certificate renewal and
the controller-locality requirements.
