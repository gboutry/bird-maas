import pytest
from maas_bird.models import Advertisements, Attachments, Inventory

from tests.prototype.render import quoted, render
from tests.prototype.resolver import InvalidIntent, NotReady, resolve


def test_matches_two_cards_without_maas_on_dpu(inputs):
    plan = resolve(**inputs)
    assert [s.interface for s in plan.sessions] == ["fabric1", "fabric2"]
    assert [s.remote_asn for s in plan.sessions] == [65011, 65012]
    assert str(plan.prefixes[0]) == "10.60.11.11/32"


def test_unrelated_dpu_does_not_change_host(inputs):
    inputs["attachments"].append(
        Attachments.model_validate(
            {"cards": [{"identity": {"scheme": "devlink-serial", "value": "other-host-card"}}]}
        )
    )
    assert len(resolve(**inputs).sessions) == 2


def test_duplicate_local_card_provider_is_rejected(inputs):
    inputs["attachments"].append(inputs["attachments"][0])
    with pytest.raises(InvalidIntent, match="Duplicate"):
        resolve(**inputs)


def test_partial_data_waits(inputs):
    inputs["inventory"] = Inventory()
    with pytest.raises(NotReady):
        resolve(**inputs)


def test_explicit_empty_snapshot_withdraws(inputs):
    inputs["advertisements"] = [Advertisements(advertisements=())]
    assert not resolve(**inputs).prefixes


def test_unknown_snapshot_retains_current_state(inputs):
    inputs["advertisements"] = [Advertisements()]
    with pytest.raises(NotReady):
        resolve(**inputs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("bfd-min-tx-ms", 49),
        ("bfd-min-rx-ms", 60001),
        ("bfd-multiplier", 0),
        ("kernel-table", 254),
        ("bgp-hold-time-seconds", 8),
    ],
)
def test_invalid_runtime_policy(inputs, field, value):
    inputs["config"][field] = value
    with pytest.raises(InvalidIntent):
        resolve(**inputs)


def test_export_allowlist(inputs):
    inputs["config"]["export-allowlist"] = "192.0.2.0/24"
    with pytest.raises(InvalidIntent, match="allowlist"):
        resolve(**inputs)


def test_unapproved_tor(inputs):
    doc = inputs["attachments"][0].model_dump()
    doc["cards"][0]["attachments"][0]["chassis_id"] = "bad-tor"
    inputs["attachments"][0] = Attachments.model_validate(doc)
    with pytest.raises(InvalidIntent, match="unapproved"):
        resolve(**inputs)


def test_renderer_uses_filters_and_extended_next_hop(inputs):
    plan = resolve(**inputs)
    config = render(plan, {"path1": "secret-a", "path2": "secret-b"})
    assert "neighbor fe80::1%fabric1 as 65011;" in config
    assert "neighbor fe80::1%fabric2 as 65012;" in config
    assert "extended next hop on;" in config
    assert "kernel table 3100;" in config
    assert "import all" not in config
    assert "persist" not in config


def test_missing_bgp_secret(inputs):
    with pytest.raises(ValueError, match="authentication"):
        render(resolve(**inputs), {})


def test_password_escaping():
    assert quoted('a"b\\c') == '"a\\"b\\\\c"'
    with pytest.raises(ValueError):
        quoted("secret\nprotocol bgp injected {}")
