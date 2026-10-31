#!/usr/bin/env python3
"""Publish a credential-minimized MAAS inventory; never manage machines."""

import hashlib
import ssl
import time
from pathlib import Path

import ops
from maas_bird.charm_support import config_string, json_list, observe, relations, secret_content
from maas_bird.interfaces import application_data, decode, publish
from maas_bird.maas import InventoryError, MaasClient, normalize_rack, restrict_cached_inventory
from maas_bird.models import Inventory, RackRequest
from maas_bird.rack import inventory_slice, rack_id
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
            machines, available = self._machines(event)
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
            scopes = dict(self._state.scopes)
            known_racks = dict(self._state.known_racks)
            # Application relation data survives leader replacement. Recover only
            # previously published bindings, never infer deletion from lost cache.
            for existing in self.model.relations["inventory"]:
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
            issues, published = [], 0
            targets = list(relations(self, "inventory", event))
            if isinstance(event, ops.RelationEvent):
                targets = [r for r in targets if r.id == event.relation.id]
            for relation in targets:
                try:
                    request = application_data(relation, RackRequest)
                    if request.rack is None:
                        continue
                    rack = rack_id(request.rack)
                    previous = scopes.get(str(relation.id))
                    if rack not in {profile["id"] for profile in racks}:
                        if previous == rack:
                            publish(relation, self.app, Inventory(rack=rack, hosts=()))
                            published += 1
                            continue
                        raise ValueError("Requested rack has no approved profile")
                    if previous is not None and previous != rack:
                        raise ValueError("Inventory relation rack is immutable; reintegrate")
                    scopes[str(relation.id)] = rack
                    self._state.scopes = scopes
                    if available:
                        inventory, bindings = normalize_rack(
                            machines, racks, identities, rack, dict(self._state.known_racks)
                        )
                        self._state.known_racks = bindings
                    else:
                        old = decode(Inventory, relation.data[self.app])
                        inventory = restrict_cached_inventory(
                            old.model_copy(update={"rack": rack}), racks, identities
                        )
                    publish(relation, self.app, inventory_slice(inventory, request))
                    published += 1
                except (ValueError, KeyError):
                    issues.append(str(relation.id))
            self.unit.status = (
                ops.BlockedStatus(
                    "Inventory retained for invalid rack relations: " + ",".join(issues)
                )
                if issues
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
            self.unit.status = ops.BlockedStatus(
                "Check MAAS secret, HTTPS URL and inventory policy"
            )

    def _machines(self, event):
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
