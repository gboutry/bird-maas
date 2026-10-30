"""Export wire schemas from the same models used by both relation endpoints."""

import json
from pathlib import Path

from maas_bird.models import Advertisements, Attachments, Inventory, InventoryRequest

ROOT = Path(__file__).resolve().parents[1]

for name, model in [
    ("machine-inventory-provider", Inventory),
    ("machine-inventory-requirer", InventoryRequest),
    ("host-attachments-provider", Attachments),
    ("route-advertisements-provider", Advertisements),
]:
    path = ROOT / "docs" / "schemas" / (name + ".json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(model.model_json_schema(mode="serialization"), indent=2) + "\n")
