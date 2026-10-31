#!/usr/bin/env python3
"""Observe existing DPU attachments without configuring the dataplane."""

import json

import ops
from maas_bird import discovery, dpu
from maas_bird.charm_support import config_string, json_list, observe
from maas_bird.interfaces import publish
from maas_bird.models import Attachments, Card, RackHeader
from maas_bird.rack import rack_id


class DpuAttachmentsCharm(ops.CharmBase):
    def __init__(self, framework):
        super().__init__(framework)
        observe(self, self._reconcile, ["attachments"])

    def _reconcile(self, event):
        try:
            rack = rack_id(self.config["rack"])
            for relation in self.model.relations["attachments"]:
                previous = relation.data[self.unit].get("rack")
                if previous is not None and previous != json.dumps(rack):
                    raise ValueError("DPU rack scope is immutable; reintegrate before moving")
                publish(relation, self.unit, RackHeader(rack=rack))
            configured = json_list(config_string(self.config, "attachments"), "attachments")
            identities = {identity.value: identity for identity in discovery.cards().values()}
            local_config = [item for item in configured if item.get("serial") in identities]
            if local_config:
                if any(
                    "host_function" not in item and "ovs_port" not in item for item in local_config
                ):
                    raise ValueError("Declare native DPU and canonical host function coordinates")
                document = dpu.observe(local_config)
            else:
                document = Attachments(
                    cards=tuple(Card(identity=i, attachments=()) for i in identities.values())
                )
            document = document.model_copy(update={"rack": rack})
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
        except (ValueError, KeyError):
            self.unit.status = ops.BlockedStatus(
                "Invalid rack or attachment policy; last observations retained"
            )
        except discovery.DiscoveryError:
            # Unknown is not an authoritative empty snapshot or a forwarding withdrawal.
            self.unit.status = ops.WaitingStatus(
                "Attachment discovery failed; last observations retained"
            )


if __name__ == "__main__":
    ops.main(DpuAttachmentsCharm)
