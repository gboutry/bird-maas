"""Export wire schemas from the same models used by both relation endpoints."""

import json
from pathlib import Path

from maas_bird.models import (
    Advertisements,
    Attachments,
    Inventory,
    InventoryRequest,
    RackRequest,
    RoutingDomain,
)
from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[1]

for name, model in [
    ("machine-inventory-provider", Inventory),
    ("machine-inventory-unit-request", InventoryRequest),
    ("machine-inventory-requirer", RackRequest),
    ("host-attachments-provider", Attachments),
    ("route-advertisements-provider", Advertisements),
]:
    path = ROOT / "docs" / "schemas" / (name + ".json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(model.model_json_schema(mode="serialization"), indent=2) + "\n")

path = ROOT / "docs/schemas/routing-domains-config.json"
path.write_text(json.dumps(TypeAdapter(list[RoutingDomain]).json_schema(), indent=2) + "\n")
