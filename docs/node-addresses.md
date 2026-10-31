# Advertise node-owned addresses

The default `advertisement-mode: workload` uses trusted workload publishers to
authorize VIP origins. Use the explicit `node-addresses` mode for addresses owned
by the host or DPU itself, where routing should not depend on one service's health.

```yaml
bird-rack-a:
  advertisement-mode: node-addresses
```

This mode uses the existing `routing-domains` context `sources` and
`export_allowlist`. It does not invent a workload relation or publish a synthetic
Ready snapshot. Every origin still requires authoritative host inventory,
matching approved attachments, a declared /32 on `lo`, provisioned source rules
and exact guard routes, valid BGP credentials, and native activation checks.
An address outside its domain export allowlist is not advertised.

Use it for node reachability, such as a host's Ceph transport address or a DPU's
Geneve endpoint, after those addresses and paths have been explicitly allocated.
The charm's status identifies node-address mode and says service health is not
checked. An established route is not evidence that an OSD, API or OVN service is
healthy. Leave shared service VIPs requiring service-dependent ownership in
workload mode with their actual readiness publisher.

## Authority and rollback

Changing modes changes the origin authority. Before trying to activate the new
policy, the charm persists a zero-export floor with the new mode and withdraws
previous origins. A failed activation cannot restore the previous authority's
VIP announcements. After native address/policy admission, the new plan can grant
its own allowed origins using the existing timed BIRD confirmation procedure.
Expect a deliberate withdrawal during this ownership transition; plan it as a
routing change, not a harmless status refresh.

In node-address mode, workload relations do not grant or revoke node origins and
their cached observations are not reused for this mode. Returning to workload
mode requires publisher data; omitting that publisher cannot inherit the previous
node-owned exports. Inventory, credentials and attachment restrictions continue
to apply in both modes.

Native checks occur at reconciliation, activation and the existing startup gate.
This option does not add a continuous service/address watchdog. An offline Juju
agent does not provide immediate revocation; the provisioning owner must keep
workload boot ordering and address/network changes coordinated with routing.

## What this does not configure

This option does not create loopbacks, peerings, VLANs, policy rules or DPU
forwarding. It does not change the source-only routing model, permit a default
route, or establish automatic source selection for ordinary host applications.
Those are separate parts of the [routed fabric proposal](routed-fabric-plan-20261031.md).
