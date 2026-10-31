#!/usr/bin/env python3
"""Publish a credential-minimized MAAS inventory; never manage machines."""

import hashlib
import ssl
import time
from collections import Counter
from pathlib import Path

import ops
from maas_bird.charm_support import config_string, json_list, observe, relations, secret_content
from maas_bird.interfaces import decode, publish
from maas_bird.maas import (
    InventoryError,
    MaasClient,
    application_scope,
    normalize_rack,
    restrict_cached_inventory,
)
from maas_bird.models import Inventory, InventoryRequest
from maas_bird.runtime import write_atomic


class MaasInventoryCharm(ops.CharmBase):
    _state = ops.StoredState()

    def __init__(self, framework):
        super().__init__(framework)
        self._state.set_default(
            scopes={},
            known_racks={},
            machines=[],
            fetched_at=0,
            cache_usable=False,
            endpoint="",
            last_attempt=0,
            api_configuration_error=False,
        )
        observe(self, self._reconcile, ["inventory"])
        framework.observe(self.on.leader_elected, self._reconcile)
        framework.observe(self.on.remove, self._remove)

    def _reconcile(self, event):
        if not self.unit.is_leader():
            self.unit.status = ops.ActiveStatus("Inventory publication is handled by the leader")
            return
        try:
            self._configuration_error = False
            active = list(relations(self, "inventory", event))
            requests = {relation.id: self._unit_requests(relation, event) for relation in active}
            force = self._needs_scope_refresh(active, requests)
            machines, available = self._machines(None if force else event)
            racks = json_list(config_string(self.config, "rack-profiles"), "rack-profiles")
            identities = json_list(
                config_string(self.config, "host-identities"), "host-identities"
            )
            # Identity allocations are global: conflicts must never be partitioned
            # away and accidentally grant the same ASN/router ID to two racks.
            for field in ("machine_id", "local_asn", "router_id"):
                values = [row[field] for row in identities]
                if len(set(values)) != len(values):
                    raise ValueError("Duplicate global routing identity allocation")
            scopes = {
                key: value
                for key, value in dict(self._state.scopes).items()
                if key in {str(relation.id) for relation in active}
            }
            known_racks = dict(self._state.known_racks)
            # Application relation data survives leader replacement. Recover only
            # previously published bindings, never infer deletion from lost cache.
            for existing in active:
                old = decode(Inventory, existing.data[self.app])
                if old.rack:
                    scopes.setdefault(str(existing.id), old.rack)
                    known_racks.update({h.machine_id: old.rack for h in old.hosts or ()})
            self._state.known_racks = known_racks
            observed_uuids = [
                m.get("hardware_uuid")
                for m in machines
                if m.get("system_id") in {i["machine_id"] for i in identities}
                and m.get("hardware_uuid")
            ]
            if len(set(observed_uuids)) != len(observed_uuids):
                raise ValueError("Duplicate global hardware identity")
            claims = Counter(uid.lower() for values in requests.values() for uid in values if uid)
            duplicates = {uid for uid, count in claims.items() if count > 1}
            issues, pending, published = [], [], 0
            targets = active
            if isinstance(event, ops.RelationEvent) and not self._fresh_snapshot:
                targets = [r for r in targets if r.id == event.relation.id]
            for relation in targets:
                try:
                    inventory = self._relation_inventory(
                        relation,
                        requests[relation.id],
                        machines,
                        available,
                        racks,
                        identities,
                        scopes,
                        duplicates,
                    )
                    publish(relation, self.app, inventory)
                    if inventory.scope_state in {"conflict", "revoked"}:
                        issues.append(str(relation.id))
                    elif inventory.scope_state == "pending":
                        pending.append(str(relation.id))
                    published += 1
                except (ValueError, KeyError):
                    self._reject_relation(relation, scopes)
                    issues.append(str(relation.id))
            self._state.scopes = scopes
            self.unit.status = (
                ops.BlockedStatus("Conflicting MAAS application membership: " + ",".join(issues))
                if issues
                else ops.WaitingStatus("Waiting for complete MAAS application membership")
                if pending and available
                else ops.ActiveStatus(f"Published {published} rack inventory slices")
                if available
                else ops.BlockedStatus(
                    "Check MAAS credentials, verified HTTPS URL and CA; only removals published"
                )
                if self._configuration_error
                else ops.WaitingStatus(
                    "MAAS unavailable; only cached authorization removals published"
                )
            )
        except InventoryError:
            self.unit.status = ops.WaitingStatus("MAAS unavailable; last inventory retained")
        except (ValueError, KeyError, ops.ModelError):
            for relation in relations(self, "inventory", event):
                self._reject_relation(relation, dict(self._state.scopes))
            self.unit.status = ops.BlockedStatus(
                "Check MAAS secret, HTTPS URL and inventory policy"
            )

    def _reject_relation(self, relation, scopes):
        old = decode(Inventory, relation.data[self.app])
        publish(
            relation,
            self.app,
            Inventory(
                rack=old.rack or scopes.get(str(relation.id)),
                hosts=(),
                member_uuids=old.member_uuids,
                scope_state="revoked" if old.scope_state == "revoked" else "conflict",
            ),
        )

    def _needs_scope_refresh(self, active, requests):
        for relation in active:
            values = requests[relation.id]
            if not values or any(not uid for uid in values):
                continue
            old = decode(Inventory, relation.data[self.app])
            members = tuple(sorted(uid.lower() for uid in values))
            if old.rack is None or old.scope_state != "ready" or old.member_uuids != members:
                return True
        return False

    def _relation_inventory(
        self, relation, requests, machines, available, racks, identities, scopes, duplicates
    ):
        old = decode(Inventory, relation.data[self.app])
        previous = scopes.get(str(relation.id))
        if old.scope_state == "revoked":
            return old.model_copy(update={"hosts": ()})
        if not available:
            restricted = restrict_cached_inventory(
                old.model_copy(update={"rack": previous}), racks, identities
            )
            return restricted.model_copy(update={"scope_state": "pending"})
        scope = application_scope(machines, racks, identities, requests, previous)
        if any(uid and uid.lower() in duplicates for uid in requests):
            return Inventory(
                rack=previous, hosts=(), member_uuids=old.member_uuids, scope_state="conflict"
            )
        if scope.state in {"rejected", "changed"}:
            return Inventory(
                rack=previous,
                hosts=(),
                member_uuids=old.member_uuids,
                scope_state="revoked" if scope.state == "changed" else "conflict",
            )
        if scope.state == "pending":
            if previous is None:
                return Inventory(scope_state="pending")
            inventory, bindings = normalize_rack(
                machines, racks, identities, previous, dict(self._state.known_racks)
            )
            self._state.known_racks = bindings
            return inventory.model_copy(
                update={
                    "member_uuids": old.member_uuids,
                    "scope_state": "pending",
                }
            )
        if (previous is None or old.member_uuids != scope.members) and not self._fresh_snapshot:
            return Inventory(rack=previous, hosts=None, scope_state="pending")
        rack = scope.rack
        assert rack is not None
        inventory, bindings = normalize_rack(
            machines, racks, identities, rack, dict(self._state.known_racks)
        )
        self._state.known_racks = bindings
        scopes[str(relation.id)] = rack
        return inventory.model_copy(
            update={
                "member_uuids": scope.members,
                "scope_state": "ready",
            }
        )

    def _unit_requests(self, relation, event):
        requests = []
        for unit in sorted(relation.units, key=lambda item: item.name):
            if (
                isinstance(event, ops.RelationDepartedEvent)
                and event.relation.id == relation.id
                and unit == event.departing_unit
            ):
                continue
            try:
                requests.append(decode(InventoryRequest, relation.data[unit]).system_uuid)
            except (ValueError, TypeError):
                requests.append(None)
        return requests

    def _machines(self, event):
        self._fresh_snapshot = False
        interval = self.config["inventory-cache-seconds"]
        if not isinstance(interval, int) or not 30 <= interval <= 3600:
            raise ValueError("Inventory refresh must be between 30 and 3600 seconds")
        now = time.time()
        cache = [
            {
                "system_id": row.get("system_id"),
                "hardware_uuid": row.get("hardware_uuid"),
                "tag_names": list(row.get("tag_names", [])),
            }
            for row in self._state.machines
        ]
        age = now - self._state.last_attempt
        endpoint = "|".join(
            str(self.config[key])
            for key in ("maas-url", "maas-credentials", "maas-ca-certificate")
        )
        if (
            isinstance(event, ops.RelationEvent)
            and self._state.endpoint == endpoint
            and self._state.last_attempt > 0
            and 0 <= age < interval
        ):
            self._configuration_error = self._state.api_configuration_error
            return cache, self._state.cache_usable
        self._state.endpoint = endpoint
        self._state.last_attempt = now
        self._state.api_configuration_error = False
        try:
            credential = secret_content(self, "maas-credentials")["api-key"]
            result = MaasClient(
                config_string(self.config, "maas-url"), credential, ca=self._ca_bundle()
            ).machines()
        except (ValueError, KeyError, ops.ModelError, ssl.SSLError):
            self._configuration_error = True
            self._state.api_configuration_error = True
            self._state.cache_usable = False
            return cache, False
        except (InventoryError, OSError):
            self._state.cache_usable = False
            return cache, False
        projected = [
            {
                "system_id": row.get("system_id"),
                "hardware_uuid": row.get("hardware_uuid"),
                "tag_names": list(row.get("tag_names", [])),
            }
            for row in result
        ]
        self._state.machines = projected
        self._state.fetched_at = now
        self._state.endpoint = endpoint
        self._state.cache_usable = True
        self._fresh_snapshot = True
        return projected, True

    def _ca_bundle(self):
        if not self.config["maas-ca-certificate"]:
            return True
        pem = secret_content(self, "maas-ca-certificate")["ca-cert"]
        ssl.create_default_context(cadata=pem)  # Parse before replacing trusted material.
        path = self._ca_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists() or path.read_text() != pem:
            write_atomic(path, pem, 0o600)
        return str(path)

    def _ca_path(self):
        owner = f"{self.model.uuid}:{self.unit.name}"
        token = hashlib.sha256(owner.encode()).hexdigest()
        return Path("/var/lib/maas-inventory") / token / "ca.crt"

    def _remove(self, event):
        path = self._ca_path()
        path.unlink(missing_ok=True)
        if path.parent.exists() and not any(path.parent.iterdir()):
            path.parent.rmdir()


if __name__ == "__main__":
    ops.main(MaasInventoryCharm)
