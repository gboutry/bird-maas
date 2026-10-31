import dataclasses
import json

import pytest
from maas_bird.models import Advertisements

from tests.prototype.render import render
from tests.prototype.resolver import InvalidIntent, NotReady, resolve


def test_contexts_use_verified_host_numa_and_keep_both_adjacent_paths(numa_inputs):
    plan = resolve(**numa_inputs)
    assert [s.numa_node for s in plan.sessions] == [0, 1]
    assert [c.table for c in plan.contexts] == [3100, 3101]
    assert [str(c.exports[0]) for c in plan.contexts] == ["10.60.11.11/32", "10.60.11.12/32"]
    config = render(plan, {"path1": "one", "path2": "two"})
    assert config.count("protocol bgp ") == 2
    assert config.count("preference = 200;") == 2
    assert config.count("preference = 100;") == 2
    assert "table path_0;" in config and "table path_1;" in config
    assert "kernel table 3100;" in config and "kernel table 3101;" in config


def test_unknown_numa_is_not_invented(numa_inputs):
    numa_inputs["local_links"][0] = dataclasses.replace(
        numa_inputs["local_links"][0], numa_node=None
    )
    with pytest.raises(NotReady, match="NUMA locality"):
        resolve(**numa_inputs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("table", 3100),
        ("id", "numa0"),
        ("rule_priority", 10000),
        ("numa_node", 2),
        ("sources", ["10.60.11.11/32"]),
    ],
)
def test_conflicting_contexts_are_rejected(numa_inputs, field, value):
    contexts = json.loads(numa_inputs["config"]["routing-contexts"])
    contexts[1][field] = value
    numa_inputs["config"]["routing-contexts"] = json.dumps(contexts)
    with pytest.raises(InvalidIntent):
        resolve(**numa_inputs)


def test_protected_destinations_cannot_be_intercepted(numa_inputs):
    numa_inputs["config"]["protected-prefixes"] = "10.60.11.0/24"
    with pytest.raises(InvalidIntent, match="protected"):
        resolve(**numa_inputs)


def test_unassigned_host_does_not_fall_back_to_legacy_mode(numa_inputs):
    contexts = json.loads(numa_inputs["config"]["routing-contexts"])
    for c in contexts:
        c["machine_id"] = "other-host"
    numa_inputs["config"]["routing-contexts"] = json.dumps(contexts)
    with pytest.raises(NotReady, match="this host"):
        resolve(**numa_inputs)


def test_advertisement_must_name_context(numa_inputs):

    doc = numa_inputs["advertisements"][0].model_dump()
    doc["advertisements"][0]["context"] = None
    numa_inputs["advertisements"] = [Advertisements.model_validate(doc)]
    with pytest.raises(NotReady, match="identify"):
        resolve(**numa_inputs)


def test_non_object_context_configuration_is_blocked(numa_inputs):
    numa_inputs["config"]["routing-contexts"] = "[3]"
    with pytest.raises(InvalidIntent, match="objects"):
        resolve(**numa_inputs)
