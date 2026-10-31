"""Typed relation APIs shared by providers and consumers.

Wire values are one JSON value per top-level field, compatible with Relation.load/save.
Collections are canonically ordered, so repeat publication is a no-op.
"""

import json
from collections.abc import MutableMapping

import ops

from maas_bird.models import Record

MAX_FIELD_BYTES = 48 * 1024


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
    # Decode only known root fields. Validate before canonicalization so unknown
    # nested extensions are discarded rather than recursively traversed by us.
    values = {}
    for key in model.model_fields:
        if key not in bag:
            continue
        if len(bag[key].encode()) > MAX_FIELD_BYTES:
            raise ValueError("Relation field exceeds the rack-scoped wire limit")
        try:
            values[key] = json.loads(bag[key])
        except RecursionError as exc:
            raise ValueError("Relation field is too deeply nested") from exc
    parsed = model.model_validate(values)
    return model.model_validate(_canonical(parsed.model_dump(mode="json", exclude_none=True)))


def publish(relation: ops.Relation, entity: ops.Application | ops.Unit, document: Record):
    bag = relation.data[entity]
    values = encode(document)
    if any(len(value.encode()) > MAX_FIELD_BYTES for value in values.values()):
        raise ValueError("Relation field exceeds the rack-scoped wire limit")
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
