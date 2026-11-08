# OVS permission for LLDP

This sudo policy lets Terraform set and read `forward-bpdu=true` on the four
named fast carrier bridges. It does not permit arbitrary OVS commands, a root
shell, service control, socket/key access or access to unrelated bridges.

LXD's network API does not expose this option. Do not give guests or
unprivileged operators access to the root-owned OVS socket to work around it.
The network module leaves authorization disabled by default. The lab profile
enables it, so an administrator must grant permission before deployment.
Terraform reapplies the option when it recreates a carrier; deleting the bridge
also deletes the setting.

Before installing the policy, check the operator account, bridge names and
client executable. MicroOVN uses `/snap/bin/microovn.ovs-vsctl`; distribution
OVS uses `/usr/bin/ovs-vsctl`. The policy and Terraform input must agree.
Do not install a policy that still names another operator's account.

From the repository root, check the adapted policy with `visudo`, then
install it:

```sh
visudo -cf tests/network-lab/terraform/host-policy/network-lab-lldp.sudoers
sudo install -o root -g root -m 0440 tests/network-lab/terraform/host-policy/network-lab-lldp.sudoers /etc/sudoers.d/maas-bird-network-lab-lldp
```

Review the policy again if you change the lab prefix. Full sudo access also
works: cache authorization with `sudo -v`, since provisioners use `sudo -n`.
An installed rule or a successful syntax check does not prove forwarding.
Check the OVS values and actual ToR LLDP reception. Check VLAN/MAC forwarding
separately from LLDP. See [compatibility](../../docs/compatibility.md) and the
[pipeline guide](../live/lab/README.md).
