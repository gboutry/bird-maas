"""Isolated Juju deployment smoke test; never enable routing on the test machines.

Run explicitly with pytest-jubilant's temporary-model fixture. This exercises packed
imports, principal placement, relation hooks, and expected missing-prerequisite states.
It does not assert BGP/BFD convergence or hardware offload.
"""

import json
from pathlib import Path

import jubilant
import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration


def test_three_principals(juju: jubilant.Juju):
    for name in ["maas-inventory", "dpu-attachments", "bird"]:
        path = ROOT / "charms" / name / f"{name}_amd64.charm"
        assert path.exists(), f"Pack {name} first"
        juju.deploy(
            str(path), app=name, config={"rack": "a"} if name != "maas-inventory" else None
        )
    juju.integrate("maas-inventory:inventory", "bird:inventory")
    juju.integrate("dpu-attachments:attachments", "bird:attachments")
    status = juju.wait(
        lambda s: (
            jubilant.all_blocked(s, "maas-inventory", "bird")
            and jubilant.all_waiting(s, "dpu-attachments")
        ),
        timeout=600,
    )
    for name in ["maas-inventory", "dpu-attachments", "bird"]:
        assert len(status.apps[name].units) == 1
        assert all(unit.machine is not None for unit in status.apps[name].units.values())


def relation_inventory(juju, app):
    """Inspect only public relation fields; never dump secrets or whole units."""
    unit = json.loads(juju.cli("show-unit", app + "/0", "--format=json"))[app + "/0"]
    entries = [r for r in unit["relation-info"] if r["endpoint"] == "inventory"]
    if not entries:
        return None
    data = entries[0].get("application-data", {})
    return json.loads(data["hosts"]) if "hosts" in data else None


def test_two_rack_https_inventory_delivery_and_removed_host(juju: jubilant.Juju, tmp_path):
    """Actual secret grants, HTTPS, hooks and rack databags; no routing enabled.

    Synthetic MAAS identities deliberately cannot authorize these LXD hosts.
    This proves delivery and containment, not a physical inventory integration.
    """
    juju.deploy(ROOT / "charms/maas-inventory/maas-inventory_amd64.charm", app="inventory")
    for rack in ("a", "b"):
        for charm, prefix in (("bird", "bird"), ("dpu-attachments", "dpu")):
            juju.deploy(
                ROOT / "charms" / charm / f"{charm}_amd64.charm",
                app=f"{prefix}-{rack}",
                config={"rack": rack},
            )
        juju.integrate("inventory:inventory", f"bird-{rack}:inventory")
        juju.integrate(f"dpu-{rack}:attachments", f"bird-{rack}:attachments")
    juju.wait(
        lambda s: (
            jubilant.all_blocked(s, "inventory", "bird-a", "bird-b")
            and jubilant.all_waiting(s, "dpu-a", "dpu-b")
        ),
        timeout=900,
    )
    juju.ssh(
        "inventory/0",
        "sudo mkdir -m 700 /tmp/maas-bird-test && sudo chown ubuntu /tmp/maas-bird-test",
    )
    juju.scp(ROOT / "tests/integration/maas_stub.py", "inventory/0:/tmp/maas-bird-test/server.py")
    machines = [
        {
            "system_id": "machine-" + rack,
            "hardware_uuid": "synthetic-" + rack,
            "tag_names": ["rack-" + rack],
            "private_metadata": "must-not-escape",
        }
        for rack in ("a", "b")
    ]
    source = tmp_path / "machines.json"
    source.write_text(json.dumps(machines))
    juju.scp(source, "inventory/0:/tmp/maas-bird-test/machines.json")
    juju.ssh(
        "inventory/0",
        "openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=maas-bird-test "
        "-addext subjectAltName=IP:127.0.0.1 -keyout /tmp/maas-bird-test/key.pem "
        "-out /tmp/maas-bird-test/ca.crt >/dev/null 2>&1 "
        "&& chmod 600 /tmp/maas-bird-test/key.pem "
        "&& sudo systemd-run --unit maas-bird-test-https "
        "python3 /tmp/maas-bird-test/server.py",
    )
    ca = juju.ssh("inventory/0", "cat /tmp/maas-bird-test/ca.crt")
    credential = juju.add_secret("test-maas", {"api-key": "test:fake:credential"})
    certificate = juju.add_secret("test-ca", {"ca-cert": ca})
    juju.grant_secret(credential, "inventory")
    juju.grant_secret(certificate, "inventory")
    profiles = [
        {
            "id": rack,
            "peers": [{"id": rack + "-tor1", "chassis_id": rack + "-chassis1", "asn": 65000 + n}],
        }
        for n, rack in enumerate(("a", "b"))
    ]
    allocations = [
        {
            "machine_id": "machine-" + rack,
            "local_asn": 4200000001 + n,
            "router_id": f"10.255.0.{n + 1}",
        }
        for n, rack in enumerate(("a", "b"))
    ]
    juju.config(
        "inventory",
        {
            "maas-url": "https://127.0.0.1:8443/MAAS/",
            "maas-credentials": str(credential),
            "maas-ca-certificate": str(certificate),
            "rack-profiles": json.dumps(profiles),
            "host-identities": json.dumps(allocations),
        },
    )
    # Consumers must issue their rack requests even while disabled for forwarding.
    for rack in ("a", "b"):
        juju.config("bird-" + rack, {"enabled": True})
    juju.wait(
        lambda s: (
            jubilant.all_active(s, "inventory") and jubilant.all_blocked(s, "bird-a", "bird-b")
        ),
        timeout=600,
    )
    for rack in ("a", "b"):
        hosts = relation_inventory(juju, "bird-" + rack)
        assert hosts is not None and len(hosts) == 1
        assert hosts[0]["machine_id"] == "machine-" + rack
        assert hosts[0]["rack"] == rack
        assert "must-not-escape" not in json.dumps(hosts)
    before_b = relation_inventory(juju, "bird-b")
    juju.config("inventory", {"host-identities": json.dumps(allocations[1:])})
    juju.wait(
        lambda s: jubilant.all_active(s, "inventory") and relation_inventory(juju, "bird-a") == [],
        timeout=300,
        successes=3,
    )
    assert relation_inventory(juju, "bird-a") == []
    assert relation_inventory(juju, "bird-b") == before_b
    for app in ("bird-a", "bird-b"):
        result = juju.exec(
            "sh",
            "-c",
            "test ! -e /etc/maas-bird/owner && ! systemctl is-active --quiet maas-bird",
            unit=app + "/0",
        )
        assert result.return_code == 0
