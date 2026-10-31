"""Dedicated path isolation and explicit non-NUMA policy for the ten-VF layout."""

import dataclasses
import json
import re
import subprocess

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from maas_bird import authority, external_policy, rack_runtime
from maas_bird.models import sessions_for_context
from maas_bird.render import protocol_name, render_rack
from maas_bird.resolver import InvalidIntent, NotReady, resolve_rack

from tests.factories import ten_vf_inputs


def render(plan):
    return render_rack(plan, {s.id: "test-only" for s in plan.sessions})


def bgp_block(text, domain, session):
    name = protocol_name(domain.domain, session.id)
    match = re.search(rf"protocol bgp {name} {{.*?\n}}\n", text, re.S)
    assert match is not None
    return match.group()


def test_ten_vfs_have_dedicated_import_and_export_paths():
    plan = resolve_rack(**ten_vf_inputs())
    text = render(plan)
    assert len(plan.sessions) == 10
    assert len({s.interface for s in plan.sessions}) == 10
    assert len(plan.contexts) == 5
    assert text.count("protocol pipe ") == 20  # One origin and one context pipe per path.
    for domain in plan.domains:
        for context in domain.contexts:
            paths = sessions_for_context(domain, context)
            assert len(paths) == 2
            assert {s.numa_node for s in paths} == {0, 1}
            for session in domain.sessions:
                block = bgp_block(text, domain, session)
                for source in context.sources:
                    assert (str(source) in block) == (session in paths)
    neutral = next(d for d in plan.domains if d.domain == "general")
    assert "preference = 200" not in render_rack(
        plan.model_copy(update={"domains": (neutral,)}),
        {s.id: "test-only" for s in neutral.sessions},
    )


def test_startup_guard_allows_only_the_contexts_dedicated_interfaces():
    plan = resolve_rack(**ten_vf_inputs())
    policy = external_policy.expected_policy(plan)
    by_table = {c["table"]: set(c["interfaces"]) for c in policy["contexts"]}
    assert by_table == {
        3100: {"vf55c0", "vf55c1"},
        3101: {"vf56c0", "vf56c1"},
        3200: {"vf57c0", "vf57c1"},
        3201: {"vf58c0", "vf58c1"},
        3300: {"vf59c0", "vf59c1"},
    }


@pytest.mark.parametrize("fault", ["missing", "wrong-domain", "duplicate-peer", "empty"])
def test_incomplete_or_ambiguous_dedicated_pair_cannot_activate(fault):
    inputs = ten_vf_inputs()
    domains = json.loads(inputs["config"]["routing-domains"])
    ids = domains[0]["contexts"][0]["attachment_ids"]
    if fault == "missing":
        ids[0] = "not-observed"
    elif fault == "wrong-domain":
        ids[0] = domains[1]["contexts"][0]["attachment_ids"][0]
    elif fault == "duplicate-peer":
        ids[1] = domains[0]["contexts"][1]["attachment_ids"][0]
    else:
        ids.clear()
    inputs["config"]["routing-domains"] = json.dumps(domains)
    with pytest.raises((InvalidIntent, NotReady)):
        resolve_rack(**inputs)


@settings(max_examples=20)
@given(nodes=st.tuples(st.sampled_from([None, 0, 1, 7]), st.sampled_from([None, 0, 1, 7])))
def test_neutral_path_policy_does_not_depend_on_reported_numa_node(nodes):
    """The general pair must not silently gain a NUMA preference on rediscovery.

    Vary both observed nodes, including unknown topology. Compare complete native
    configurations: a local preference, community or pipe change would be visible.
    Ceph's strict locality checks remain independently enforced below.
    """
    inputs = ten_vf_inputs()
    expected = render(resolve_rack(**inputs))
    neutral_links = [x for x in inputs["local_links"] if x.vf == 59]
    replacements = {
        link.interface: dataclasses.replace(link, numa_node=node)
        for link, node in zip(neutral_links, nodes, strict=True)
    }
    inputs["local_links"] = [replacements.get(x.interface, x) for x in inputs["local_links"]]
    assert render(resolve_rack(**inputs)) == expected


def test_ceph_still_requires_known_local_numa_attachment():
    inputs = ten_vf_inputs()
    inputs["local_links"][0] = dataclasses.replace(inputs["local_links"][0], numa_node=None)
    with pytest.raises(NotReady, match="NUMA locality is unknown"):
        resolve_rack(**inputs)


@settings(max_examples=20)
@given(domain_index=st.integers(0, 1), context_index=st.integers(0, 1), path=st.integers(0, 1))
def test_path_reassignment_commits_only_the_old_new_intersection(
    domain_index, context_index, path
):
    """A new path is a grant, never a rollback target for an already revoked path.

    Reassign one Ceph context to the other context's same-TOR path. The restricted
    floor must immediately lose the revoked pipe and VIP origin, keep the surviving
    old path, and never use the proposed replacement before acceptance.
    """
    inputs = ten_vf_inputs()
    previous = resolve_rack(**inputs)
    domains = json.loads(inputs["config"]["routing-domains"])
    domain = domains[domain_index]
    context = domain["contexts"][context_index]
    old_ids = set(context["attachment_ids"])
    replacement = domain["contexts"][1 - context_index]["attachment_ids"][path]
    removed = context["attachment_ids"][path]
    context["attachment_ids"][path] = replacement
    config = inputs["config"] | {"routing-domains": json.dumps(domains)}
    floor = authority.restrict(previous, config, inputs["inventory"], [])
    narrowed = next(d for d in floor.domains if d.domain == domain["id"])
    selected = next(c for c in narrowed.contexts if c.id == context["id"])
    assert {s.id for s in sessions_for_context(narrowed, selected)} == old_ids - {removed}
    assert replacement not in selected.attachment_ids
    assert not selected.exports
    assert authority.restrict(floor, config, inputs["inventory"], []) == floor


@pytest.mark.parametrize("selected_node", [0, 1])
def test_neutral_health_does_not_report_either_valid_path_as_numa_fallback(
    selected_node, monkeypatch, tmp_path
):
    plan = resolve_rack(**ten_vf_inputs())
    service = rack_runtime.RackService(tmp_path)
    monkeypatch.setattr(
        service,
        "_control",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )
    by_table = {}
    for domain in plan.domains:
        for context in domain.contexts:
            node = context.numa_node if context.numa_node is not None else selected_node
            path = next(s for s in sessions_for_context(domain, context) if s.numa_node == node)
            by_table[context.table] = [
                {"dst": str(p), "dev": path.interface} for p in domain.imports
            ]
    monkeypatch.setattr(
        rack_runtime,
        "run",
        lambda args: subprocess.CompletedProcess(args, 0, json.dumps(by_table[int(args[-1])]), ""),
    )
    assert service.health(plan)["fallback_routes"] == 0
