import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from maas_bird.interfaces import decode, encode
from maas_bird.models import Advertisements, Attachments, Inventory, MultiDomainPlan, RoutingDomain
from pydantic import TypeAdapter

from tests.prototype.policy import layout
from tests.prototype.render import render
from tests.prototype.resolver import InvalidIntent, NotReady, resolve


def test_domains_keep_sessions_routes_and_timers_separate(domain_inputs):
    plan = resolve(**domain_inputs)
    assert isinstance(plan, MultiDomainPlan)
    assert [d.domain for d in plan.domains] == ["storage-public", "storage-replica"]
    assert [d.bfd_tx for d in plan.domains] == [50, 300]
    config = render(plan, {s.id: "example" for s in plan.sessions})
    assert config.count("protocol bfd ") == 1
    assert config.count("protocol bgp ") == 4
    assert "table d0_context_0;" in config and "table d1_context_0;" in config
    assert 'proto = "uplink_2"' in config
    policy = layout(plan)
    assert policy["tables"] == [3100, 3101, 3200, 3201]
    assert len(policy["rules"]) == 8
    assert {r["dst"] for r in policy["rules"] if r["src"] == "10.60.11.11/32"} == {
        "203.0.113.0/24",
        "198.51.100.0/24",
    }
    assert {r["table"] for r in policy["rules"] if r["src"] == "10.60.11.11/32"} == {3100}


@pytest.mark.parametrize(
    "change",
    [
        "table",
        "priority",
        "imports",
        "exports",
        "id",
        "legacy",
        "interface",
        "session",
        "source",
        "timers",
        "missing-host",
        "protected",
    ],
)
def test_domain_conflicts_are_rejected_before_rendering(domain_inputs, change):
    config = domain_inputs["config"]
    domains = json.loads(config["routing-domains"])
    if change == "table":
        domains[1]["contexts"][0]["table"] = 3100
    elif change == "priority":
        # Different base, but the cross-domain guard consumes the adjacent priority.
        domains[1]["contexts"][0]["rule_priority"] = 10001
    elif change == "imports":
        domains[1]["import_prefixes"] = ["203.0.113.128/25"]
    elif change == "exports":
        domains[1]["export_allowlist"] = ["10.60.11.0/25"]
    elif change == "id":
        domains[1]["id"] = domains[0]["id"]
    elif change == "legacy":
        config["routing-contexts"] = json.dumps(domains[0]["contexts"])
    elif change in {"interface", "session"}:
        doc = domain_inputs["attachments"][0].model_dump(mode="json")
        if change == "interface":
            doc["cards"][0]["attachments"][1].update(
                {"function": {"controller": 0, "pf": 0, "vf": 63}, "vlan": 3414}
            )
        else:
            doc["cards"][0]["attachments"][1]["id"] = "path1"
        domain_inputs["attachments"][0] = type(domain_inputs["attachments"][0]).model_validate(doc)
    elif change == "source":
        domains[1]["contexts"][0]["sources"] = ["10.60.11.11/32"]
    elif change == "timers":
        domains[1]["bgp_hold_time_seconds"] = 3
    elif change == "missing-host":
        for context in domains[1]["contexts"]:
            context["machine_id"] = "other-host"
    else:
        config["protected-prefixes"] = "198.51.100.0/24"
    config["routing-domains"] = json.dumps(domains)
    with pytest.raises((InvalidIntent, NotReady)):
        resolve(**domain_inputs)


def test_unknown_operator_fields_are_rejected_but_relations_stay_extensible(domain_inputs):

    doc = domain_inputs["advertisements"][0].model_dump(mode="json")
    doc["advertisements"][0]["future-field"] = True
    parsed = Advertisements.model_validate(doc)
    roundtrip = decode(Advertisements, encode(parsed))
    assert encode(roundtrip) == encode(domain_inputs["advertisements"][0])
    assert roundtrip.advertisements is not None
    assert {(a.domain, a.context, str(a.prefix)) for a in roundtrip.advertisements} == {
        ("storage-public", "numa0", "10.60.11.11/32"),
        ("storage-public", "numa1", "10.60.11.12/32"),
        ("storage-replica", "numa0", "172.22.0.11/32"),
        ("storage-replica", "numa1", "172.22.0.12/32"),
    }
    domains = json.loads(domain_inputs["config"]["routing-domains"])
    domains[1]["bfd_min_tx_mss"] = 50
    domain_inputs["config"]["routing-domains"] = json.dumps(domains)
    with pytest.raises(ValueError, match="bfd_min_tx_mss"):
        resolve(**domain_inputs)


def test_domain_withdrawal_does_not_withdraw_other_domain(domain_inputs):

    doc = domain_inputs["advertisements"][0].model_dump(mode="json")
    for advertisement in doc["advertisements"]:
        if advertisement["domain"] == "storage-replica":
            advertisement["ready"] = False
    domain_inputs["advertisements"] = [Advertisements.model_validate(doc)]
    plan = resolve(**domain_inputs)
    assert isinstance(plan, MultiDomainPlan)
    assert len(plan.domains[0].prefixes) == 2
    assert plan.domains[1].prefixes == ()
    config = render(plan, {s.id: "example" for s in plan.sessions})
    assert "172.22.0.11/32" not in config
    assert "10.60.11.11/32" in config
    assert len(layout(plan)["rules"]) == 8


def test_concrete_relation_and_config_examples_match_exported_schemas():

    root = Path(__file__).resolve().parents[2]
    examples = root / "tests/prototype/examples/multiple-domains"
    for name, model, schema in [
        ("inventory", Inventory, "machine-inventory-provider"),
        ("attachments-card1", Attachments, "host-attachments-provider"),
        ("attachments-card2", Attachments, "host-attachments-provider"),
        ("advertisements", Advertisements, "route-advertisements-provider"),
    ]:
        doc = json.loads((examples / (name + ".json")).read_text())
        jsonschema.validate(
            doc, json.loads((root / "docs/schemas" / (schema + ".json")).read_text())
        )
        value = model.model_validate(doc)
        roundtrip = decode(model, encode(value))
        assert encode(roundtrip) == encode(value)
        if model is Advertisements:
            assert isinstance(roundtrip, Advertisements)
            assert roundtrip.advertisements is not None
            assert {a.domain for a in roundtrip.advertisements} == {
                "storage-public",
                "storage-replica",
            }
            assert {a.context for a in roundtrip.advertisements} == {"numa0", "numa1"}
    config = yaml.safe_load((examples / "bird.yaml").read_text())["bird"]
    domains = json.loads(config["routing-domains"])
    jsonschema.validate(
        domains, json.loads((root / "docs/schemas/routing-domains-config.json").read_text())
    )
    assert len(TypeAdapter(list[RoutingDomain]).validate_python(domains)) == 2


def test_domain_policy_identity_ignores_configuration_order(domain_inputs):
    domains = json.loads(domain_inputs["config"]["routing-domains"])
    domains[0]["import_prefixes"].append("203.0.114.0/24")
    domains[0]["contexts"][0]["sources"].append("10.60.11.13/32")
    domain_inputs["config"]["routing-domains"] = json.dumps(domains)
    first = layout(resolve(**domain_inputs))
    domains[0]["import_prefixes"].reverse()
    domains[0]["contexts"][0]["sources"].reverse()
    domains[0]["contexts"].reverse()
    domains.reverse()
    domain_inputs["config"]["routing-domains"] = json.dumps(domains)
    assert layout(resolve(**domain_inputs)) == first
