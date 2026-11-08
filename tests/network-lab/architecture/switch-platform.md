# Switches and kernels

The intended physical switches use NVIDIA Cumulus Linux. Choose the Cumulus
release, vendor FRR build, ASIC and firmware before testing physical switches.
The host distribution does not tell us which kernel a Cumulus switch will run.

The lab runs upstream FRR 10.7.1 in Ubuntu system containers. Those containers
share the LXD host kernel. The two management access containers provide
IP-less Linux bridging. Neither environment runs Cumulus NVUE, `switchd`, the
NVIDIA SDK or an ASIC dataplane.

The lab exercises IPv4 /32 advertisements over IPv6 link-local BGP, multipath
routes, VRFs and EVPN/VXLAN. Physical switches still need tests for vendor
configuration, hashing, neighbor programming and recovery. A Cumulus virtual
image would need separate compatibility tests; it would not establish ASIC
behavior.

Workers and logical gateways use Ubuntu Noble HWE. Hosts encapsulate Geneve
and select an outgoing link before packets reach the switch, so changing the
switch to Cumulus does not settle host tunnel distribution. DPU encapsulation
needs its own tests. See [known limitations](../docs/limitations.md) and
[physical acceptance requirements](../docs/validation.md).
