#!/usr/bin/env python3
"""Observe native local DPU attachments independently of application rack placement."""

import ops
from maas_bird import discovery, dpu
from maas_bird.charm_support import config_string, json_list, observe, relations
from maas_bird.interfaces import publish
from maas_bird.models import Attachments, Card


class DpuAttachmentsCharm(ops.CharmBase):
    def __init__(self, framework):
        super().__init__(framework)
        observe(self, self._reconcile, ["attachments"])

    def _reconcile(self, event):
        try:
            # Observation is local to this card, independent of the application's
            # rack placement. Consumers join native card identity to MAAS peers.
            document = self._observations()
            for relation in relations(self, "attachments", event):
                try:
                    publish(relation, self.unit, document)
                except ops.RelationNotFoundError:
                    # Juju can remove this relation between the departed event
                    # snapshot and relation-set. Other publication failures must
                    # remain visible, as must a missing unrelated relation.
                    if not (
                        isinstance(event, ops.RelationDepartedEvent)
                        and event.relation.id == relation.id
                    ):
                        raise
            ready = [a.ready for card in document.cards or () for a in card.attachments or ()]
            self.unit.status = (
                ops.ActiveStatus("Attachments validated against devlink, OVS and LLDP")
                if ready and all(ready)
                else ops.WaitingStatus("Card identity published; attachments not ready")
            )
        except (ValueError, KeyError):
            self.unit.status = ops.BlockedStatus(
                "Invalid attachment policy; last observations retained"
            )
        except discovery.DiscoveryError:
            self.unit.status = ops.WaitingStatus(
                "Attachment discovery failed; last observations retained"
            )

    def _observations(self):
        configured = json_list(config_string(self.config, "attachments"), "attachments")
        identities = {identity.value: identity for identity in discovery.cards().values()}
        local_config = [item for item in configured if item.get("serial") in identities]
        if local_config:
            if any(
                "host_function" not in item and "ovs_port" not in item for item in local_config
            ):
                raise ValueError("Declare native DPU and canonical host coordinates")
            return dpu.observe(local_config)
        return Attachments(
            cards=tuple(
                Card(identity=identity, attachments=()) for identity in identities.values()
            )
        )


if __name__ == "__main__":
    ops.main(DpuAttachmentsCharm)
