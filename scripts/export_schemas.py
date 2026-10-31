"""Export wire schemas from the same models used by both relation endpoints."""

import json
from pathlib import Path

from maas_bird.models import (
    Advertisements,
    Attachments,
    Inventory,
    InventoryRequest,
    Record,
    RoutingDomain,
    UnitInventory,
)
from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[1]

for name, model in [
    ("inventory-snapshot", Inventory),
    ("machine-inventory-unit-response", UnitInventory),
    ("machine-inventory-unit-request", InventoryRequest),
    ("machine-inventory-requirer", Record),
    ("host-attachments-requirer", Record),
    ("host-attachments-provider", Attachments),
    ("route-advertisements-provider", Advertisements),
]:
    path = ROOT / "docs" / "schemas" / (name + ".json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(model.model_json_schema(mode="serialization"), indent=2) + "\n")

path = ROOT / "docs/schemas/routing-domains-config.json"
path.write_text(json.dumps(TypeAdapter(list[RoutingDomain]).json_schema(), indent=2) + "\n")

unit_schema = UnitInventory.model_json_schema(mode="serialization")
provider_schema = {
    "title": "Unit-addressed inventory provider application data",
    "type": "object",
    "$defs": unit_schema.pop("$defs", {}),
    "patternProperties": {
        "^unit-[0-9]+$": {
            "type": "string",
            "maxLength": 48 * 1024,
            "contentMediaType": "application/json",
            "contentSchema": unit_schema,
        },
    },
    "additionalProperties": False,
}
(ROOT / "docs/schemas/machine-inventory-provider.json").write_text(
    json.dumps(provider_schema, indent=2) + "\n"
)
