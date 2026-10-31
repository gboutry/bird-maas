#!/usr/bin/env python3
"""Publish a credential-minimized MAAS inventory; never manage machines."""

import hashlib
import re
import ssl
import time
from collections import Counter
from pathlib import Path

import ops
from maas_bird.charm_support import config_string, json_list, observe, relations, secret_content
from maas_bird.interfaces import decode
from maas_bird.inventory import publish_response, read_response, response_key
from maas_bird.maas import InventoryError, MaasClient, unit_inventory
from maas_bird.models import InventoryRequest, UnitInventory
from maas_bird.runtime import write_atomic


class MaasInventoryCharm(ops.CharmBase):
    _state = ops.StoredState()
    _configuration_error: bool = False

    def __init__(self, framework):
        super().__init__(framework)
        self._state.set_default(
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
        active = list(relations(self, "inventory", event))
        claims = {relation.id: self._unit_requests(relation, event) for relation in active}
        try:
            force = self._needs_scope_refresh(active, claims)
            machines, available = self._machines(None if force else event)
            racks = json_list(config_string(self.config, "rack-profiles"), "rack-profiles")
            identities = json_list(
                config_string(self.config, "host-identities"), "host-identities"
            )
            counts = Counter(
                request.system_uuid.lower()
                for units in claims.values()
                for request in units.values()
                if request and request.system_uuid
            )
            issues, pending, published = [], [], 0
            # A fresh snapshot can change any unit. Cached relation events only
            # service their own relation, without multiplying ordinary API reads.
            targets = active
            if isinstance(event, ops.RelationEvent) and not self._fresh_snapshot:
                targets = [r for r in active if r.id == event.relation.id]
            for relation in targets:
                units = claims[relation.id]
                for name, request in units.items():
                    previous = self._previous(relation, name)
                    uid = request.system_uuid.lower() if request and request.system_uuid else None
                    document = unit_inventory(
                        machines,
                        racks,
                        identities,
                        request,
                        previous,
                        name,
                        available=available,
                        fresh=self._fresh_snapshot,
                        duplicate=uid is not None and counts[uid] > 1,
                    )
                    publish_response(relation, self.app, document)
                    if document.scope_state in {"conflict", "revoked"}:
                        issues.append(name)
                    elif document.scope_state == "pending":
                        pending.append(name)
                    published += 1
                self._prune(relation, units)
            self.unit.status = (
                ops.BlockedStatus("Conflicting MAAS unit identities: " + ",".join(issues))
                if issues
                else ops.WaitingStatus("Waiting for MAAS unit identity")
                if pending and available
                else ops.ActiveStatus(f"Published {published} unit inventories")
                if available
                else ops.BlockedStatus(
                    "Check MAAS credentials, verified HTTPS URL and CA; only removals published"
                )
                if self._configuration_error
                else ops.WaitingStatus(
                    "MAAS unavailable; only cached authorization removals published"
                )
            )
        except (ValueError, KeyError, ops.ModelError):
            for relation in active:
                for name in claims[relation.id]:
                    old = self._previous(relation, name)
                    publish_response(
                        relation,
                        self.app,
                        old.model_copy(
                            update={
                                "host": None,
                                "scope_state": "revoked"
                                if old.scope_state == "revoked"
                                else "conflict",
                            }
                        ),
                    )
                self._prune(relation, claims[relation.id])
            self.unit.status = ops.BlockedStatus(
                "Check MAAS secret, HTTPS URL and inventory policy"
            )

    def _previous(self, relation, name):
        try:
            return read_response(relation.data[self.app], name)
        except ValueError:
            # Corrupt accepted authority is never evidence for a fresh binding.
            return UnitInventory(unit_name=name, scope_state="revoked")

    def _prune(self, relation, units):
        wanted = {response_key(name) for name in units}
        for key in list(relation.data[self.app]):
            if re.fullmatch(r"unit-[0-9]+", key) and key not in wanted:
                del relation.data[self.app][key]

    def _needs_scope_refresh(self, active, claims):
        for relation in active:
            for name, request in claims[relation.id].items():
                if not request or not request.system_uuid:
                    continue
                old = self._previous(relation, name)
                if old.rack is None or old.system_uuid != request.system_uuid.lower():
                    return True
        return False

    def _unit_requests(self, relation, event):
        result = {}
        for unit in sorted(relation.units, key=lambda item: item.name):
            if (
                isinstance(event, ops.RelationDepartedEvent)
                and event.relation.id == relation.id
                and unit == event.departing_unit
            ):
                continue
            try:
                result[unit.name] = decode(InventoryRequest, relation.data[unit])
            except (ValueError, TypeError):
                result[unit.name] = None
        return result

    def _machines(self, event):
        self._configuration_error = False
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
