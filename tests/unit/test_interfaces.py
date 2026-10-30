import json

import pytest
from maas_bird.interfaces import decode, encode
from maas_bird.models import Advertisements, Attachments, Inventory, InventoryRequest
from pydantic import ValidationError


@pytest.mark.parametrize("model", [Inventory, InventoryRequest, Attachments, Advertisements])
def test_empty_and_future_fields(model):
    assert decode(model, {}) == model()
    assert decode(model, {"future": "not-even-json", "ingress-address": "10.0.0.1"}) == model()


def test_old_and_new_inventory_vectors():
    old = {"hosts": json.dumps([{"machine_id": "abc123"}])}
    new = {"hosts": json.dumps([{"machine_id": "abc123", "new_field": "ignored"}])}
    assert decode(Inventory, old) == decode(Inventory, new)
    assert decode(Inventory, {"hosts": "null"}).hosts is None


def test_stable_collection_order():
    a = Inventory.model_validate({"hosts": [{"machine_id": "b"}, {"machine_id": "a"}]})
    b = Inventory.model_validate({"hosts": [{"machine_id": "a"}, {"machine_id": "b"}]})
    assert encode(a) == encode(b)


def test_unknown_is_not_explicit_withdrawal():
    assert encode(Advertisements()) == {}
    assert encode(Advertisements(advertisements=())) == {"advertisements": "[]"}


@pytest.mark.parametrize("value", ["false", 0, 1])
def test_readiness_is_not_coerced(value):
    with pytest.raises(ValidationError):
        Advertisements.model_validate({"advertisements": [{"ready": value}]})


@pytest.mark.parametrize("value", ["2001:db8::1", "fe80::1%eth0", "127.0.0.1"])
def test_only_unscoped_link_local_peers(value):
    with pytest.raises(ValidationError):
        Attachments.model_validate({"cards": [{"attachments": [{"peer_link_local": value}]}]})


@pytest.mark.parametrize("value", [True, "65001", 0, 4294967295])
def test_asn_is_strict_and_bounded(value):
    with pytest.raises(ValidationError):
        Inventory.model_validate({"hosts": [{"local_asn": value}]})


def test_duplicate_collection_members_are_discarded():
    a = decode(Inventory, {"hosts": '[{"machine_id":"a"},{"machine_id":"a"}]'})
    assert len(a.hosts) == 1
