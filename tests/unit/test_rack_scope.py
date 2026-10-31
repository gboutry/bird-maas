"""Host-local selection and revocation remain independent of other producers."""

import pytest
from maas_bird.interfaces import encode
from maas_bird.models import Advertisements
from maas_bird.rack import (
    advertisements_for_host,
    attachments_for_host,
)

from tests.factories import rack_inputs


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
        attachments_for_host(encode(unknown), {card.identity})
    denied = doc.model_copy(
        update={
            "cards": (
                card.model_copy(
                    update={"attachments": (attachment.model_copy(update={"ready": False}),)}
                ),
            )
        }
    )
    assert attachments_for_host(encode(denied), {card.identity}) == denied
