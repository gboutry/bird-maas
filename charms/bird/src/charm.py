#!/usr/bin/env python3
"""Generic principal BIRD operator consuming typed inventory and attachment contracts."""

import json
from ipaddress import IPv4Interface

import ops
from maas_bird import discovery, runtime
from maas_bird.charm_support import observe, relations, secret_content
from maas_bird.interfaces import application_data, decode, publish
from maas_bird.models import (
    Advertisements,
    Attachments,
    Inventory,
    InventoryRequest,
    MultiDomainPlan,
)
from maas_bird.policy import PolicyError
from maas_bird.render import render
from maas_bird.resolver import AuthorizationWithdrawn, NotReady, resolve


class BirdCharm(ops.CharmBase):
    _state = ops.StoredState()

    def __init__(self, framework):
        super().__init__(framework)
        self._state.set_default(dependencies=[])
        observe(self, self._reconcile, ["inventory", "attachments", "advertisements"])
        framework.observe(self.on.remove, self._remove)

    def _reconcile(self, event):
        service = runtime.BirdService(owner=f"{self.model.uuid}:{self.unit.name}")
        if not self.config["enabled"]:
            service.stop()
            self.unit.status = ops.BlockedStatus(
                "Set enabled=true after provisioning fabric interfaces"
            )
            return
        dependency = None
        if isinstance(event, ops.RelationBrokenEvent):
            dependency = f"relation:{event.relation.id}"
        elif isinstance(event, ops.RelationDepartedEvent):
            dependency = f"unit:{event.relation.id}:{event.departing_unit.name}"
        inventory_removed = (
            isinstance(event, ops.RelationBrokenEvent) and event.relation.name == "inventory"
        )
        if inventory_removed or (dependency and dependency in self._state.dependencies):
            try:
                service.invalidate()
            finally:
                service.stop()
            self.unit.status = ops.BlockedStatus("A trusted routing producer was removed")
            return
        try:
            uid = discovery.host_uuid()
            inv_relations = list(relations(self, "inventory", event))
            if len(inv_relations) != 1:
                raise NotReady("Waiting for MAAS inventory relation")
            inv_relation = inv_relations[0]
            publish(inv_relation, self.unit, InventoryRequest(system_uuid=uid))
            inventory = application_data(inv_relation, Inventory)
            attachments = []
            advertisements = []
            for endpoint, model, destination in [
                ("attachments", Attachments, attachments),
                ("advertisements", Advertisements, advertisements),
            ]:
                for relation in relations(self, endpoint, event):
                    for unit in sorted(relation.units, key=lambda u: u.name):
                        if isinstance(event, ops.RelationDepartedEvent) and (
                            event.relation.id == relation.id and event.departing_unit == unit
                        ):
                            continue

                        destination.append(decode(model, relation.data[unit]))
            local_links = discovery.links()
            plan = resolve(
                uid, inventory, attachments, advertisements, local_links, dict(self.config)
            )
            addresses = json.loads(discovery.command(["ip", "-j", "-4", "address", "show", "lo"]))
            local = {
                IPv4Interface(f"{a['local']}/{a['prefixlen']}").network
                for iface in addresses
                for a in iface.get("addr_info", [])
            }
            if any(prefix not in local for prefix in plan.prefixes):
                raise NotReady("Advertised service /32 is not present on lo")
            secrets = secret_content(self, "bgp-credentials")
            contents = render(plan, secrets)
            dependencies = [f"relation:{inv_relation.id}"]
            identities = {link.identity for link in local_links}
            for endpoint in ["attachments", "advertisements"]:
                for relation in relations(self, endpoint, event):
                    for unit in relation.units:
                        if endpoint == "attachments":
                            doc = decode(Attachments, relation.data[unit])
                            relevant = any(
                                c.identity and c.identity in identities for c in doc.cards or ()
                            )
                        else:
                            doc = decode(Advertisements, relation.data[unit])
                            relevant = any(
                                a.machine_id == plan.host.machine_id
                                for a in doc.advertisements or ()
                            )
                        if relevant:
                            dependencies.extend(
                                [f"relation:{relation.id}", f"unit:{relation.id}:{unit.name}"]
                            )
            self._state.dependencies = sorted(set(dependencies))
            if isinstance(plan, MultiDomainPlan) or plan.contexts:
                service.apply_contexts(contents, plan)
            else:
                service.apply(
                    contents, plan.kernel_table, len(plan.sessions), exports=plan.prefixes
                )
            self.unit.status = ops.ActiveStatus(f"{len(plan.sessions)} BGP sessions established")
        except AuthorizationWithdrawn as exc:
            try:
                service.invalidate()
            finally:
                service.stop()
            self.unit.status = ops.BlockedStatus(str(exc))
        except (NotReady, runtime.SessionsPending) as exc:
            self.unit.status = ops.WaitingStatus(str(exc))
        except (ValueError, KeyError, ops.ModelError):
            self.unit.status = ops.BlockedStatus("Invalid routing input or missing BGP secret")
        except (discovery.DiscoveryError, OSError):
            self.unit.status = ops.WaitingStatus(
                "Host discovery unavailable; last configuration retained"
            )
        except (runtime.RuntimeErrorBase, PolicyError) as exc:
            self.unit.status = ops.BlockedStatus(str(exc))

    def _remove(self, event):
        runtime.BirdService(owner=f"{self.model.uuid}:{self.unit.name}").remove()


if __name__ == "__main__":
    ops.main(BirdCharm)
