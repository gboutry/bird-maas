#!/usr/bin/env python3
"""Observe existing DPU attachments in the consumer's authoritative MAAS scope."""

import ops
from maas_bird import discovery, dpu
from maas_bird.charm_support import config_string, json_list, observe, relations
from maas_bird.interfaces import application_data, decode, publish
from maas_bird.models import Attachments, Card, RackHeader, RackRequest
from maas_bird.rack import rack_id


class DpuAttachmentsCharm(ops.CharmBase):
    _state = ops.StoredState()

    def __init__(self, framework):
        super().__init__(framework)
        self._state.set_default(scopes={})
        observe(self, self._reconcile, ["attachments"])

    def _reconcile(self, event):
        active = list(relations(self, "attachments", event))
        scopes = {
            key: value
            for key, value in dict(self._state.scopes).items()
            if key in {str(relation.id) for relation in active}
        }
        targets, rejected = [], []
        for relation in active:
            try:
                rack = self._delivery_scope(relation, scopes)
                if rack is not None:
                    targets.append((relation, rack))
                elif str(relation.id) in scopes:
                    rejected.append(relation.id)
            except (ValueError, KeyError):
                self._withdraw(relation, scopes.get(str(relation.id)))
                rejected.append(relation.id)
        self._state.scopes = scopes
        if not targets:
            self.unit.status = (
                ops.BlockedStatus("Attachment scope changed; reintegrate relation")
                if rejected
                else ops.WaitingStatus("Awaiting MAAS-derived consumer scope")
            )
            return
        try:
            document = self._observations()
            for relation, rack in targets:
                publish(relation, self.unit, document.model_copy(update={"rack": rack}))
                scopes[str(relation.id)] = rack
            self._state.scopes = scopes
            ready = [a.ready for card in document.cards or () for a in card.attachments or ()]
            self.unit.status = (
                ops.BlockedStatus("An attachment relation changed scope; reintegrate it")
                if rejected
                else ops.ActiveStatus("Attachments validated against devlink, OVS and LLDP")
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

    def _delivery_scope(self, relation, scopes):
        old = decode(RackHeader, relation.data[self.unit]).rack
        previous = scopes.get(str(relation.id), old)
        if previous is not None:
            previous = rack_id(previous)
            scopes[str(relation.id)] = previous
        requested = application_data(relation, RackRequest).rack
        if requested is None:
            return previous  # Unknown delivery context cannot grant a new scope.
        requested = rack_id(requested)
        if previous is not None and previous != requested:
            self._withdraw(relation, previous)
            return None
        return requested

    def _withdraw(self, relation, rack):
        if rack is None:
            return
        old = decode(Attachments, relation.data[self.unit])
        publish(
            relation,
            self.unit,
            Attachments(
                rack=rack,
                cards=tuple(
                    Card(identity=card.identity, attachments=()) for card in old.cards or ()
                ),
            ),
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
