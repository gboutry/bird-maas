#!/usr/bin/env python3
"""Publish a credential-minimized MAAS inventory; never manage machines."""

import ops
from maas_bird.charm_support import json_list, observe, secret_content
from maas_bird.interfaces import publish
from maas_bird.maas import InventoryError, MaasClient, normalize


class MaasInventoryCharm(ops.CharmBase):
    def __init__(self, framework):
        super().__init__(framework)
        observe(self, self._reconcile, ["inventory"])
        framework.observe(self.on.leader_elected, self._reconcile)

    def _reconcile(self, event):
        if not self.unit.is_leader():
            self.unit.status = ops.ActiveStatus("Inventory publication is handled by the leader")
            return
        try:
            content = secret_content(self, "maas-credentials")
            if not self.config["maas-url"]:
                raise ValueError("Set maas-url to a verified HTTPS endpoint")
            inventory = normalize(
                MaasClient(self.config["maas-url"], content["api-key"]).machines(),
                json_list(self.config["rack-profiles"], "rack-profiles"),
                json_list(self.config["host-identities"], "host-identities"),
            )
            for relation in self.model.relations["inventory"]:
                publish(relation, self.app, inventory)
            self.unit.status = ops.ActiveStatus(f"Published {len(inventory.hosts or ())} hosts")
        except InventoryError:
            self.unit.status = ops.WaitingStatus("MAAS unavailable; last inventory retained")
        except (ValueError, KeyError, ops.ModelError):
            self.unit.status = ops.BlockedStatus(
                "Check MAAS secret, HTTPS URL and inventory policy"
            )


if __name__ == "__main__":
    ops.main(MaasInventoryCharm)
