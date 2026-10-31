#!/usr/bin/env python3
"""Rack-scoped principal BIRD operator; static host policy is externally owned."""

import json
from ipaddress import IPv4Interface

import ops
from maas_bird import authority, discovery, external_policy
from maas_bird.charm_support import config_string, json_list, observe, relations, secret_content
from maas_bird.interfaces import application_data, publish
from maas_bird.models import Inventory, InventoryRequest, RackRequest, advertisement_mode
from maas_bird.observations import collect
from maas_bird.rack import ScopeError, rack_id
from maas_bird.rack_runtime import RackService
from maas_bird.resolver import AuthorizationWithdrawn, NotReady, resolve_rack
from maas_bird.runtime import RuntimeErrorBase, SessionsPending


class BirdCharm(ops.CharmBase):
    _state = ops.StoredState()

    def __init__(self, framework):
        super().__init__(framework)
        self._state.set_default(observations={})
        observe(self, self._reconcile, ["inventory", "attachments", "advertisements"])
        framework.observe(self.on.leader_elected, self._reconcile)
        framework.observe(self.on.remove, self._remove)
        for name in ("inspect", "plan", "reconcile", "decommission"):
            framework.observe(getattr(self.on, name + "_action"), self._action)

    def _service(self):
        return RackService(owner=f"{self.model.uuid}:{self.unit.name}")

    def _inventory(self, event, *, publish_requests=True):
        inventory_relations = list(relations(self, "inventory", event))
        if len(inventory_relations) != 1:
            raise NotReady("Waiting for one authoritative inventory relation")
        relation = inventory_relations[0]
        try:
            uid = discovery.host_uuid()
        except (OSError, discovery.DiscoveryError):
            if publish_requests:
                publish(relation, self.unit, InventoryRequest())
            raise
        if publish_requests:
            publish(relation, self.unit, InventoryRequest(system_uuid=uid))
            if self.unit.is_leader():
                publish(relation, self.app, RackRequest())  # Retire old consumer-selected scope.
        inventory = application_data(relation, Inventory)
        if inventory.scope_state in {"conflict", "revoked"} or inventory.hosts == ():
            raise AuthorizationWithdrawn("MAAS application scope was withdrawn")
        if inventory.scope_state not in {"ready", "pending"} or inventory.rack is None:
            raise NotReady("Waiting for MAAS-derived application scope")
        rack = rack_id(inventory.rack)
        previous = self._service().state()
        if previous is not None and previous.host.system_uuid != uid:
            raise AuthorizationWithdrawn("Local hardware identity changed")
        if previous is not None and previous.host.rack != rack:
            raise ScopeError("Accepted rack scope changed; reintegrate inventory")
        if uid not in (inventory.member_uuids or ()):
            if previous is not None:
                raise AuthorizationWithdrawn("Unit membership withdrawn from MAAS scope")
            raise NotReady("Waiting for this unit's authoritative MAAS membership")
        if publish_requests and self.unit.is_leader() and inventory.scope_state == "ready":
            for attachment in relations(self, "attachments", event):
                publish(attachment, self.app, RackRequest(rack=rack))
        return rack, relation, uid

    def _inputs(self, event, *, publish_requests=True):
        rack, relation, uid = self._inventory(event, publish_requests=publish_requests)
        inventory = application_data(relation, Inventory)
        if inventory.rack is None:
            raise NotReady("Waiting for rack-scoped inventory")
        if inventory.rack != rack:
            raise ScopeError("Inventory relation has the wrong rack scope")
        hosts = [h for h in inventory.hosts or () if h.system_uuid == uid]
        if inventory.hosts is not None and not hosts:
            raise AuthorizationWithdrawn("Host routing identity withdrawn from inventory")
        if len(hosts) != 1 or hosts[0].rack != rack:
            raise NotReady("Waiting for one host in this physical rack")
        discovery_warnings = []
        try:
            links = discovery.links()
            ovs_ports = json_list(config_string(self.config, "local-ovs-ports"), "local-ovs-ports")
            if ovs_ports:
                links += discovery.ovs_links(ovs_ports)
            identities = {link.identity for link in links}
        except (discovery.DiscoveryError, ValueError, KeyError):
            # Discovery failure may block grants, never known revocations. Reuse
            # only accepted card identities to select local producer snapshots.
            previous = self._service().state()
            identities = (
                {s.identity for s in previous.sessions if s.identity} if previous else set()
            )
            links = None
            discovery_warnings.append(
                "Local link discovery unavailable; no new adjacencies granted"
            )
        documents, cache, warnings = collect(
            self,
            event,
            rack,
            hosts[0].machine_id,
            identities,
            dict(self._state.observations) or self._service().observations(),
            workload_mode=advertisement_mode(self.config) == "workload",
        )
        inputs = dict(
            uid=uid,
            inventory=inventory,
            attachments=documents["attachments"],
            advertisements=documents["advertisements"],
            local_links=links,
            config=dict(self.config),
        )
        return inputs, cache, warnings + discovery_warnings

    def _reconcile(self, event):
        service = self._service()
        if not self.config["enabled"]:
            service.stop()
            try:
                # Disabled deployment can prove real inventory delivery without
                # packages, attachment probes, credentials or routing activation.
                self._inventory(event)
            except (NotReady, ValueError, OSError, discovery.DiscoveryError):
                pass
            self.unit.status = ops.BlockedStatus(
                "Routing disabled; provision host policy then enable"
            )
            return
        try:
            if isinstance(event, ops.RelationBrokenEvent) and event.relation.name == "inventory":
                raise AuthorizationWithdrawn("The authoritative inventory relation was removed")
            service.verify_current(check_bindings=False)
            inputs, cache, warnings = self._inputs(event)
            if not self.config["bgp-credentials"]:
                raise AuthorizationWithdrawn("BGP credentials were withdrawn")
            try:
                passwords = secret_content(self, "bgp-credentials")
            except ops.SecretNotFoundError as exc:
                raise AuthorizationWithdrawn("BGP credential authorization was removed") from exc
            previous = service.state()
            if previous is not None:
                # Static source guards must hold even for a malformed proposal.
                # A known logical-port revocation commits its restrictive floor
                # before verifying the remaining accepted OVS UUID bindings.
                restricted = authority.restrict(
                    previous,
                    inputs["config"],
                    inputs["inventory"],
                    inputs["advertisements"],
                    passwords,
                    inputs["attachments"],
                    local_links=inputs["local_links"],
                )
                if restricted != previous or service.credentials_changed(restricted, passwords):
                    service.apply_plan(restricted, passwords, restrictive=True, observations=cache)
                service.verify_current()
            self._state.observations = cache
            plan = resolve_rack(**inputs)
            self._check_addresses(plan)
            service.apply_plan(plan, passwords, observations=cache)
            health = service.health(plan)
            message = (
                f"Rack {plan.host.rack}: {health['established']}/{health['configured']} BGP/BFD Up"
            )
            if plan.advertisement_mode == "node-addresses":
                message += "; node addresses, service health not checked"
            degraded = (
                health["established"] != health["configured"]
                or health["missing_routes"]
                or health["fallback_routes"]
            )
            self.unit.status = (
                ops.WaitingStatus(message + "; degraded routing or local inputs")
                if warnings or degraded
                else ops.ActiveStatus(message)
            )
        except (AuthorizationWithdrawn, ScopeError) as exc:
            try:
                service.invalidate()
            finally:
                service.stop()
            self.unit.status = ops.BlockedStatus(str(exc))
        except (NotReady, SessionsPending, discovery.DiscoveryError) as exc:
            self.unit.status = ops.WaitingStatus(str(exc))
        except (ValueError, KeyError, ops.ModelError):
            self.unit.status = ops.BlockedStatus(
                "Invalid local routing policy or unavailable secret"
            )
        except OSError:
            self.unit.status = ops.BlockedStatus("Host/workload I/O failed; inspect routing state")
        except (RuntimeErrorBase, external_policy.PolicyError) as exc:
            self.unit.status = ops.BlockedStatus(str(exc))

    @staticmethod
    def _check_addresses(plan):
        addresses = json.loads(discovery.command(["ip", "-j", "-4", "address", "show", "lo"]))
        local = {
            IPv4Interface(f"{address['local']}/{address['prefixlen']}").network
            for interface in addresses
            for address in interface.get("addr_info", [])
        }
        if any(source not in local for context in plan.contexts for source in context.sources):
            raise NotReady("Provision every declared source VIP as a /32 on lo")

    def _action(self, event):
        try:
            if event.handle.kind == "decommission_action":
                if self.config["enabled"] or event.params.get("confirm") is not True:
                    event.fail("Set enabled=false and confirm=true before decommissioning")
                    return
                self._service().remove()
                event.set_results(
                    {"result": "Owned service removed; external host policy retained"}
                )
            elif event.handle.kind == "reconcile_action":
                self._reconcile(event)
                event.set_results(
                    {"status": self.unit.status.name, "message": self.unit.status.message}
                )
            else:
                plan = (
                    self._service().state()
                    if event.handle.kind == "inspect_action"
                    else resolve_rack(**self._inputs(event, publish_requests=False)[0])
                )
                event.set_results(
                    {"plan": json.dumps(plan.model_dump(mode="json") if plan else {})}
                )
        except (ValueError, RuntimeErrorBase, NotReady, OSError, discovery.DiscoveryError) as exc:
            event.fail(str(exc))

    def _remove(self, event):
        self._service().remove()


if __name__ == "__main__":
    ops.main(BirdCharm)
