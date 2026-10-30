"""Typed relation APIs shared by providers and consumers.

Wire values are one JSON value per top-level field, compatible with Relation.load/save.
Collections are canonically ordered, so repeat publication is a no-op.
"""

import json
from collections.abc import MutableMapping

import ops

from maas_bird.models import Record


def _json_key(value):
    return json.dumps(value, sort_keys=True)


def _canonical(value):
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        items = {_json_key(_canonical(item)): _canonical(item) for item in value}
        return [items[key] for key in sorted(items)]
    return value


def encode(document: Record) -> dict[str, str]:
    return {
        key: json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"))
        for key, value in document.model_dump(mode="json", exclude_none=True).items()
    }


def decode[T: Record](model: type[T], bag: MutableMapping[str, str]) -> T:
    # Juju also adds unrelated fields such as ingress-address: do not decode those.
    return model.model_validate(
        {key: _canonical(json.loads(bag[key])) for key in model.model_fields if key in bag}
    )


def publish(relation: ops.Relation, entity: ops.Application | ops.Unit, document: Record):
    bag = relation.data[entity]
    values = encode(document)
    for key in document.__class__.model_fields:
        if key in values:
            if bag.get(key) != values[key]:
                bag[key] = values[key]
        elif key in bag:
            del bag[key]


def application_data[T: Record](relation: ops.Relation, model: type[T]) -> T:
    return decode(model, relation.data[relation.app]) if relation.app else model()


def unit_data[T: Record](relation: ops.Relation, model: type[T]) -> list[T]:
    return [
        decode(model, relation.data[unit]) for unit in sorted(relation.units, key=lambda x: x.name)
    ]
