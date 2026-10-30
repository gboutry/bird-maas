from unittest.mock import Mock

import pytest
import requests
from maas_bird.maas import InventoryError, MaasClient, normalize


@pytest.fixture
def records():
    return (
        [
            {
                "system_id": "abc123",
                "hardware_uuid": "HOST-UUID",
                "tag_names": ["rack-a"],
                "power_parameters": {"password": "never-export"},
                "user_data": "never-export",
            }
        ],
        [{"id": "a", "peers": [{"id": "tor1", "chassis_id": "chassis1", "asn": 65011}]}],
        [{"machine_id": "abc123", "local_asn": 4200001011, "router_id": "10.255.11.11"}],
    )


def test_normalize_only_allowlisted_fields(records):
    result = normalize(*records)
    assert result.hosts[0].system_uuid == "host-uuid"
    assert "never-export" not in result.model_dump_json()
    assert "power_parameters" not in result.model_dump_json()


def test_no_ownership_changes(records):
    original = records[0][0].copy()
    normalize(*records)
    assert records[0][0] == original


@pytest.mark.parametrize("tags", [[], ["rack-a", "rack-b"], ["rack-unknown"]])
def test_rack_is_explicit(records, tags):
    records[0][0]["tag_names"] = tags
    with pytest.raises(ValueError, match="rack"):
        normalize(*records)


def test_allocated_machine_missing(records):
    with pytest.raises(ValueError, match="missing"):
        normalize([], records[1], records[2])


@pytest.mark.parametrize(
    "url",
    [
        "http://maas/MAAS/",
        "https://user:pass@maas/MAAS/",
        "https://maas/MAAS/?token=x",
        "https://maas/MAAS/#x",
    ],
)
def test_no_unsafe_credential_transport(url):
    with pytest.raises(ValueError):
        MaasClient(url, "consumer:token:secret")


def test_real_get_boundary_uses_tls_and_no_redirects(monkeypatch):
    get = Mock(return_value=Mock(status_code=200, json=lambda: []))
    monkeypatch.setattr("maas_bird.maas.requests.get", get)
    assert MaasClient("https://maas/MAAS/", "consumer:token:secret").machines() == []
    assert get.call_args.args == ("https://maas/MAAS/api/2.0/machines/",)
    assert get.call_args.kwargs["verify"] is True
    assert get.call_args.kwargs["allow_redirects"] is False


def test_errors_do_not_leak_credential(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.RequestException("sensitive header token:secret")

    monkeypatch.setattr("maas_bird.maas.requests.get", fail)
    with pytest.raises(InventoryError) as exc:
        MaasClient("https://maas/MAAS/", "consumer:token:secret").machines()
    assert "secret" not in str(exc.value)


def test_certificate_verification_cannot_be_disabled():
    with pytest.raises(ValueError, match="cannot be disabled"):
        MaasClient("https://maas/MAAS/", "consumer:token:secret", ca=False)
