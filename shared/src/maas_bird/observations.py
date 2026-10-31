"""Select and cache only producers bound to this host inside a rack relation.

Unknown or invalid snapshots preserve that producer's previous observation. An
explicit empty snapshot replaces it. Departures remove only the departing
producer's authority; unrelated units cannot stall a local withdrawal.
"""

import ops

from maas_bird.charm_support import relations
from maas_bird.interfaces import decode
from maas_bird.models import Advertisements, Attachments, Card, RackHeader
from maas_bird.rack import advertisements_for_host, attachments_for_host


def collect(charm, event, rack, machine_id, identities, previous, *, workload_mode=True):
    cache = dict(previous)
    documents = {"attachments": [], "advertisements": []}
    live = set()
    warnings = []
    for endpoint in documents:
        if endpoint == "advertisements" and not workload_mode:
            continue
        for relation in relations(charm, endpoint, event):
            for unit in sorted(relation.units, key=lambda u: u.name):
                if isinstance(event, ops.RelationDepartedEvent) and (
                    relation.id == event.relation.id and unit == event.departing_unit
                ):
                    continue
                key = f"{endpoint}:{relation.id}:{unit.name}"
                live.add(key)
                try:
                    bag = relation.data[unit]
                    document = (
                        advertisements_for_host(bag, rack, machine_id)
                        if endpoint == "advertisements"
                        else attachments_for_host(bag, rack, identities)
                    )
                    if (
                        endpoint == "attachments"
                        and key in cache
                        and decode(RackHeader, bag).rack == rack
                    ):
                        observed = decode(Attachments, bag)
                        old = Attachments.model_validate(cache[key])
                        if observed.cards is not None:
                            present = {card.identity for card in observed.cards}
                            retained = [
                                card for card in observed.cards if card.identity in identities
                            ]
                            retained += [
                                Card(identity=card.identity, attachments=())
                                for card in old.cards or ()
                                if card.identity not in present
                            ]
                            document = observed.model_copy(update={"cards": tuple(retained)})
                    if document is not None:
                        if (
                            isinstance(document, Advertisements)
                            and document.advertisements is None
                        ):
                            warnings.append("A local workload snapshot is unavailable")
                        else:
                            cache[key] = document.model_dump(mode="json")
                except (ValueError, TypeError):
                    warnings.append("A local producer has invalid or incomplete data")
                if key in cache:
                    model = Advertisements if endpoint == "advertisements" else Attachments
                    documents[endpoint].append(model.model_validate(cache[key]))
    # Keep tombstone snapshots for a removed workload's domain in THIS compile.
    # They revoke its old exports even when all publisher units have departed.
    for key, raw in previous.items():
        if workload_mode and key not in live and key.startswith("advertisements:"):
            documents["advertisements"].append(
                Advertisements.model_validate(raw).model_copy(update={"advertisements": ()})
            )
        if key not in live and key.startswith("attachments:"):
            old = Attachments.model_validate(raw)
            documents["attachments"].append(
                old.model_copy(
                    update={
                        "cards": tuple(
                            Card(identity=card.identity, attachments=())
                            for card in old.cards or ()
                        )
                    }
                )
            )
    cache = {key: value for key, value in cache.items() if key in live}
    domains = [doc.domain for doc in documents["advertisements"]]
    duplicates = {domain for domain in domains if domains.count(domain) > 1}
    if duplicates:
        # Conflicting authority cannot grant exports, but must not block a valid
        # withdrawal in another domain. Quarantine only the disputed domains.
        documents["advertisements"] = [
            doc.model_copy(update={"advertisements": ()}) if doc.domain in duplicates else doc
            for doc in documents["advertisements"]
        ]
        warnings.append("Conflicting local workload producers; disputed exports withdrawn")
    return documents, cache, warnings
