"""Compile the reviewed rack allocation without asserting live readiness.

These observations exist only in the test process. Production examples contain
intent, never a relation databag that an operator could publish as Ready.
"""

import json
from ipaddress import IPv6Address
from pathlib import Path

import pytest
import yaml
from maas_bird.discovery import LocalLink
from maas_bird.models import Attachment, Attachments, Card, Host, Identity, Inventory
from maas_bird.render import render_rack
from maas_bird.resolver import resolve_rack

EXAMPLES = Path(__file__).resolve().parents[2] / "examples/rb3-rack-local"


def load(name):
    return json.loads((EXAMPLES / name).read_text())


def reviewed_host_inputs(name):
    """Supply explicit test observations for the proposed, unprovisioned links."""
    host = next(h for h in load("hosts.json") if h["name"] == name)
    config = yaml.safe_load((EXAMPLES / "host-bird.yaml").read_text())["mbird-acceptance-rb3"]
    inventory = yaml.safe_load((EXAMPLES / "inventory.yaml").read_text())[
        "mbird-acceptance-inventory"
    ]
    profile = json.loads(inventory["rack-profiles"])[0]
    rack = profile["id"]
    peers = profile["peers"]
    configured = json.loads(
        yaml.safe_load((EXAMPLES / "dpu-attachments.yaml").read_text())[
            "mbird-acceptance-dpu-rb3"
        ]["attachments"]
    )
    by_id = {a["id"]: a for a in configured}
    documents, links = [], []
    for side in range(2):
        paths = [p for p in host["paths"] if p["numa_card"] == side]
        serial = by_id[paths[0]["session_id"]]["serial"]
        identity = Identity(scheme="devlink-serial", value=serial)
        attachments = []
        for path in paths:
            data = by_id[path["session_id"]]
            attachments.append(
                Attachment.model_validate(
                    {**data, "function": data["host_function"], "ready": True}
                )
            )
            links.append(
                LocalLink(
                    interface=path["interface"],
                    identity=identity,
                    controller=0,
                    pf=0,
                    vf=path["vf"],
                    vlan=path["vlan"],
                    link_local=IPv6Address(path["local_link_local"]),
                    numa_node=side,
                )
            )
        documents.append(Attachments(cards=(Card(identity=identity, attachments=attachments),)))
    uid = f"test-observation-{name}"
    return dict(
        uid=uid,
        inventory=Inventory(
            rack=rack,
            scope_state="ready",
            hosts=(
                Host(
                    machine_id=host["machine_id"],
                    system_uuid=uid,
                    rack=rack,
                    local_asn=host["asn"],
                    router_id=host["router_id"],
                    peers=peers,
                ),
            ),
        ),
        attachments=documents,
        advertisements=[],
        local_links=links,
        config=config,
    )


@pytest.mark.parametrize("host", ["n1", "n5", "n6"])
def test_reviewed_ceph_config_compiles_with_test_observations(host):
    inputs = reviewed_host_inputs(host)
    assert inputs["config"]["enabled"] is False
    assert "rack" not in inputs["config"]
    plan = resolve_rack(**inputs)
    assert plan.advertisement_mode == "node-addresses"
    assert len(plan.sessions) == 8
    assert len(plan.contexts) == 4
    assert len(plan.prefixes) == 4
    assert {domain.domain for domain in plan.domains} == {"ceph-public", "ceph-replication"}
    text = render_rack(plan, {s.id: "test-process-only" for s in plan.sessions})
    assert text.count("protocol bgp ") == 8
    assert "graceful restart off;" in text


def test_eight_slot_host_and_dpu_ranges_cannot_collide():
    allocation = load("rack-allocation.json")
    host_vlans = [v for h in allocation["host_slots"] for v in h["vlans"]]
    dpu_vlans = [v for d in allocation["dpu_slots"] for v in d["vlans"]]
    assert host_vlans == list(range(3500, 3580))
    assert dpu_vlans == list(range(3580, 3612))
    assert len(set(host_vlans + dpu_vlans)) == 112
    for h in load("hosts.json"):
        expected = allocation["host_slots"][h["slot"] - 1]["vlans"]
        assert [p["vlan"] for p in h["paths"]] == expected
        assert h["general_source_addresses"] is None


def test_tor_intent_accounts_for_every_host_and_dpu_adjacency():
    hosts, dpus = load("hosts.json"), load("dpus.json")
    for tor in (1, 2):
        intent = load(f"tor{tor}-intent.json")
        expected = {p["session_id"] for h in hosts for p in h["paths"] if p["tor"] == tor}
        assert {p["session_id"] for p in intent["host_peers"]} == expected
        assert len(expected) == 15
        assert {p["machine"] for p in intent["dpu_peers"]} == {d["machine"] for d in dpus}
        assert len(intent["dpu_peers"]) == 6
        assert intent["bfd"] == {"tx_ms": 300, "rx_ms": 300, "multiplier": 3}
