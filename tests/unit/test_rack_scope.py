"""Rack partitioning is a delivery boundary, not a JSON host filter illusion."""

from ipaddress import IPv4Address

import pytest
from hypothesis import given
from hypothesis import strategies as st
from maas_bird.interfaces import encode
from maas_bird.models import Advertisements, Host, Inventory, RackRequest
from maas_bird.rack import (
    ScopeError,
    advertisements_for_host,
    attachments_for_host,
    inventory_slice,
)

from tests.factories import rack_inputs


@given(rack_count=st.integers(2, 60), hosts_per_rack=st.integers(1, 40))
def test_slice_contains_exactly_one_rack(rack_count, hosts_per_rack):
    """Vary fleet dimensions; each relation's payload is independent of fleet size.

    This checks content and isolation. It is not a Juju hook-count or scale test.
    """
    hosts = tuple(
        Host(
            machine_id=f"m{r}-{h}",
            system_uuid=f"u{r}-{h}",
            rack=f"r{r}",
            local_asn=4200000000 + r * 40 + h,
            router_id=IPv4Address(0x0AFF0000 + r * 40 + h),
        )
        for r in range(rack_count)
        for h in range(hosts_per_rack)
    )
    result = inventory_slice(Inventory(hosts=hosts), RackRequest(rack="r1"))
    assert result.rack == "r1"
    assert result.hosts == tuple(host for host in hosts if host.rack == "r1")
    assert len(result.hosts) == hosts_per_rack


@pytest.mark.parametrize("rack", [None, "", "a b", "a/b", "a\n"])
def test_invalid_rack_cannot_select_a_slice(rack):
    with pytest.raises(ScopeError):
        inventory_slice(Inventory(hosts=()), RackRequest(rack=rack))


def test_unknown_and_empty_host_snapshots_remain_distinct():
    header = {"rack": '"r1"', "machine_id": '"host1"', "domain": '"storage-public"'}
    assert advertisements_for_host(header, "r1", "host1").advertisements is None
    withdrawn = Advertisements(
        rack="r1", machine_id="host1", domain="storage-public", advertisements=()
    )
    assert advertisements_for_host(encode(withdrawn), "r1", "host1") == withdrawn
    assert advertisements_for_host(header | {"advertisements": "not-json"}, "r1", "other") is None
    assert (
        advertisements_for_host(header | {"advertisements": "not-json"}, "other", "host1") is None
    )


def test_deep_or_oversized_unrelated_headers_cannot_poison_selection():
    deep = "[" * 2000 + "0" + "]" * 2000
    for invalid in (deep, '"' + "x" * (49 * 1024) + '"'):
        assert advertisements_for_host({"rack": invalid}, "a", "local") is None


def test_unknown_attachment_readiness_is_not_an_authoritative_revocation():
    inputs = rack_inputs()
    doc = inputs["attachments"][0]
    card = doc.cards[0]
    attachment = card.attachments[0].model_copy(update={"ready": None})
    unknown = doc.model_copy(
        update={"cards": (card.model_copy(update={"attachments": (attachment,)}),)}
    )
    with pytest.raises(ValueError, match="incomplete"):
        attachments_for_host(encode(unknown), "a", {card.identity})
    denied = doc.model_copy(
        update={
            "cards": (
                card.model_copy(
                    update={"attachments": (attachment.model_copy(update={"ready": False}),)}
                ),
            )
        }
    )
    assert attachments_for_host(encode(denied), "a", {card.identity}) == denied
