"""Bounded, unit-addressed MAAS inventory responses in provider application data.

Juju providers cannot write a remote unit's databag. One application field per
remote unit avoids copying every rack's inventory into a single oversized value.
"""

import json
import re

from maas_bird.interfaces import MAX_FIELD_BYTES, encode
from maas_bird.models import UnitInventory
from maas_bird.rack import ScopeError


def response_key(unit_name: str) -> str:
    match = re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]*/([0-9]+)", unit_name)
    if match is None:
        raise ValueError("Invalid Juju consumer unit name")
    return "unit-" + match.group(1)


def response_data(document: UnitInventory) -> dict[str, str]:
    if not document.unit_name:
        raise ValueError("Inventory response must identify its consumer unit")
    value = json.dumps(
        {key: json.loads(value) for key, value in encode(document).items()},
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(value.encode()) > MAX_FIELD_BYTES:
        raise ValueError("Unit inventory response exceeds the wire limit")
    return {response_key(document.unit_name): value}


def read_response(bag, unit_name: str) -> UnitInventory:
    value = bag.get(response_key(unit_name))
    if value is None:
        return UnitInventory(unit_name=unit_name, scope_state="pending")
    if len(value.encode()) > MAX_FIELD_BYTES:
        raise ValueError("Unit inventory response exceeds the wire limit")
    try:
        document = UnitInventory.model_validate_json(value)
    except RecursionError as exc:
        raise ValueError("Unit inventory response is too deeply nested") from exc
    if document.unit_name != unit_name:
        raise ScopeError("Inventory response belongs to another Juju unit")
    return document


def publish_response(relation, app, document: UnitInventory):
    for key, value in response_data(document).items():
        if relation.data[app].get(key) != value:
            relation.data[app][key] = value
