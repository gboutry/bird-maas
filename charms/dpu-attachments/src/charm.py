#!/usr/bin/env python3
"""Observe existing DPU attachments without configuring the dataplane."""

import ops
from maas_bird import discovery, dpu
from maas_bird.charm_support import json_list, observe
from maas_bird.interfaces import publish
from maas_bird.models import Attachments, Card


class DpuAttachmentsCharm(ops.CharmBase):
    def __init__(self, framework):
        super().__init__(framework)
        observe(self, self._reconcile, ["attachments"])

    def _reconcile(self, event):
        try:
            configured = json_list(self.config["attachments"], "attachments")
            identities = {identity.value: identity for identity in discovery.cards().values()}
            local_config = [item for item in configured if item.get("serial") in identities]
            if local_config:
                document = dpu.observe(local_config)
            else:
                document = Attachments(cards=tuple(Card(identity=i) for i in identities.values()))
            for relation in self.model.relations["attachments"]:
                publish(relation, self.unit, document)
            ready = [a.ready for c in document.cards or () for a in c.attachments or ()]
            if ready and all(ready):
                self.unit.status = ops.ActiveStatus(
                    "Attachments validated against devlink, OVS and LLDP"
                )
            else:
                self.unit.status = ops.WaitingStatus(
                    "Card identity published; attachments not ready"
                )
        except (ValueError, KeyError, discovery.DiscoveryError):
            # Unknown is not an authoritative empty snapshot or a forwarding withdrawal.
            self.unit.status = ops.WaitingStatus(
                "Attachment discovery failed; last observations retained"
            )


if __name__ == "__main__":
    ops.main(DpuAttachmentsCharm)
