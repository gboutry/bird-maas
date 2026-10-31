"""Explicit logical OVS identity, local discovery and durable revocation admission.

These fixtures model native command output. They do not stand in for a physical
DOCA/offload test; native BIRD parsing is exercised separately.
"""

import dataclasses
import json
import subprocess
from ipaddress import IPv6Address
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from maas_bird import authority, discovery, dpu, external_policy, ovsdb
from maas_bird.interfaces import decode, encode
from maas_bird.models import Attachments, Identity, OvsPort
from maas_bird.rack import attachments_for_host
from maas_bird.render import render_rack
from maas_bird.resolver import InvalidIntent, NotReady, resolve_rack

from tests.factories import ovs_inputs, ovs_port_binding, rack_inputs
from tests.factories import ovs_snapshot_text as snapshot_text
from tests.rack_host_model import ProvisionedHost


def test_single_native_snapshot_normalizes_ovs_uuid_and_set_atoms():
    binding = ovs_port_binding()
    calls = []

    def execute(args):
        calls.append(args)
        return snapshot_text(binding)

    state = ovsdb.snapshot(execute)
    assert ovsdb.internal_port(state, "br-data", "svi3500") == binding.model_dump(
        mode="json", exclude={"uplink"}
    )
    assert len(calls) == 1
    assert calls[0].count("list") == 3
    assert not {"add", "set", "del", "create", "destroy"} & set(calls[0])


@pytest.mark.parametrize(
    "fault", ["type", "tag", "trunks", "ifindex", "ofport", "error", "bridge", "members"]
)
def test_internal_port_requires_exact_supported_topology(fault):
    binding = ovs_port_binding()
    state = ovsdb.snapshot(lambda _: snapshot_text(binding))
    if fault == "type":
        state["Interface"][0]["type"] = "geneve"
    elif fault == "tag":
        state["Port"][0]["tag"] = []
    elif fault == "trunks":
        state["Port"][0]["trunks"] = [3501]
    elif fault in {"ifindex", "ofport"}:
        state["Interface"][0][fault] = -1
    elif fault == "error":
        state["Interface"][0]["error"] = "not installed"
    elif fault == "bridge":
        state["Bridge"][0]["ports"] = []
    else:
        state["Port"][0]["interfaces"] *= 2
    with pytest.raises(ValueError):
        ovsdb.internal_port(state, "br-data", "svi3500")


@pytest.mark.parametrize(
    "fault", [None, "foreign-card", "bridge", "ifindex", "missing", "ovs-bond"]
)
def test_explicit_internal_discovery_joins_card_ovs_and_kernel_identity(
    tmp_path, monkeypatch, fault
):
    binding = ovs_port_binding()
    identity = Identity(scheme="devlink-serial", value="card-a")
    pf = tmp_path / "devices/0000:03:00.0"
    pf.mkdir(parents=True)
    uplink = tmp_path / "class/net/p0"
    uplink.mkdir(parents=True)
    (uplink / "device").symlink_to(pf)
    monkeypatch.setattr(discovery, "cards", lambda: {pf.name: identity})
    calls = []
    documents = [json.loads(line) for line in snapshot_text(binding).splitlines()]
    if fault == "foreign-card":
        foreign = tmp_path / "devices/0000:04:00.0"
        foreign.mkdir()
        (uplink / "device").unlink()
        (uplink / "device").symlink_to(foreign)
    elif fault == "bridge":
        documents[0]["data"][0][2] = ["set", []]
    elif fault == "missing":
        documents[1]["data"][0][1] = "another-port"
    elif fault == "ovs-bond":
        documents[1]["data"][1][2] = [
            "set",
            [["uuid", str(binding.uplink.interface_uuid)], ["uuid", str(UUID(int=99))]],
        ]

    def command(args):
        calls.append(args)
        if args[0] == "ovs-vsctl":
            return "\n".join(json.dumps(document) for document in documents)
        assert args == ["ip", "-j", "-d", "address", "show"]
        return json.dumps(
            [
                {
                    "ifname": "svi3500",
                    "ifindex": 32 if fault == "ifindex" else 31,
                    "addr_info": [{"family": "inet6", "scope": "link", "local": "fe80::2"}],
                }
            ]
        )

    monkeypatch.setattr(discovery, "command", command)
    links = discovery.ovs_links(
        [{"serial": "card-a", "bridge": "br-data", "port": "svi3500", "uplink": "p0"}], tmp_path
    )
    assert not {"add", "set", "del"} & {arg for args in calls for arg in args}
    if fault is not None:
        assert links == []  # Authoritative known absence, not unknown discovery.
        return
    assert len(links) == 1
    assert links[0].identity == identity
    assert links[0].ovs_port == binding
    assert (links[0].controller, links[0].pf, links[0].vf) == (None, None, None)
    assert links[0].link_local == IPv6Address("fe80::2")
    assert not {"add", "set", "del"} & {arg for args in calls for arg in args}


def test_provider_publishes_known_missing_internal_port_as_not_ready(monkeypatch):
    monkeypatch.setattr(discovery, "ovs_links", lambda _: [])
    item = {
        "serial": "card-a",
        "id": "underlay-a",
        "domain": "underlay",
        "vlan": 3500,
        "ovs_port": {"bridge": "br-data", "port": "svi3500"},
        "uplink": "p0",
        "chassis_id": "aa:aa:aa:aa:aa:01",
        "peer_link_local": "fe80::1",
    }
    observed = dpu._logical_attachment(item, {})
    assert observed.ready is False
    assert observed.function is None
    assert observed.ovs_port.port == "svi3500"


def test_nested_selector_cannot_override_top_level_uplink_intent(monkeypatch):
    monkeypatch.setattr(discovery, "ovs_links", lambda _: pytest.fail("discovery reached"))
    with pytest.raises(ValueError, match="only bridge and port"):
        dpu._logical_attachment(
            {"ovs_port": {"bridge": "br-data", "port": "svi3500", "uplink_bond": "unapproved"}}, {}
        )


@pytest.mark.parametrize("fault", ["missing-column", "extra-column", "map-atom", "truncated"])
def test_malformed_snapshot_is_unknown_not_a_topology_absence(fault):
    documents = [json.loads(line) for line in snapshot_text(ovs_port_binding()).splitlines()]
    if fault == "missing-column":
        documents[0]["headings"].pop()
    elif fault == "extra-column":
        documents[0]["headings"].append("unexpected")
    elif fault == "map-atom":
        documents[0]["data"][0][0] = ["map", []]
    else:
        documents.pop()
    with pytest.raises(ValueError) as raised:
        ovsdb.snapshot(lambda _: "\n".join(json.dumps(document) for document in documents))
    assert not isinstance(raised.value, ovsdb.TopologyError)


def test_empty_optin_never_queries_ovs(monkeypatch):
    monkeypatch.setattr(discovery, "command", lambda _: pytest.fail("unexpected native query"))
    assert discovery.ovs_links([]) == []


def test_ovs_relation_roundtrip_and_resolver_preserve_native_identity():
    inputs = ovs_inputs()
    for document in inputs["attachments"]:
        assert encode(decode(Attachments, encode(document))) == encode(document)
        selected = attachments_for_host(
            encode(document), {link.identity for link in inputs["local_links"]}
        )
        assert selected == document
    plan = resolve_rack(**inputs)
    assert all(s.function is None and s.ovs_port.complete for s in plan.sessions)
    text = render_rack(plan, {s.id: "test-only" for s in plan.sessions})
    assert text.count("protocol bgp ") == 4


def test_logical_link_cannot_invent_physical_coordinates():
    with pytest.raises(ValueError, match="cannot invent"):
        discovery.LocalLink(
            "svi3500",
            Identity(value="card-a"),
            0,
            0,
            None,
            3500,
            IPv6Address("fe80::2"),
            ovs_port=ovs_port_binding(),
        )


def test_physical_selector_cannot_match_logical_local_link():
    inputs = ovs_inputs()
    inputs["attachments"] = rack_inputs()["attachments"]
    with pytest.raises(NotReady):
        resolve_rack(**inputs)


def test_mixed_selectors_are_rejected():
    inputs = ovs_inputs()
    raw = inputs["attachments"][0].model_dump(mode="json")
    raw["cards"][0]["attachments"][0]["function"] = {"controller": 0, "pf": 0}
    inputs["attachments"][0] = Attachments.model_validate(raw)
    with pytest.raises(InvalidIntent, match="mixes"):
        resolve_rack(**inputs)


@settings(max_examples=25)
@given(
    replacement=st.uuids(), field=st.sampled_from(["bridge_uuid", "port_uuid", "interface_uuid"])
)
def test_recreated_ovs_objects_are_new_authority_not_a_stale_rollback(replacement, field):
    """Vary one native object identity without changing its name or VLAN.

    Keeping the other metadata fixed catches accidental name-only adoption. The
    restrictive floor must lose the old session before any replacement grant,
    and applying the same restriction twice must not resurrect it.
    """
    inputs = ovs_inputs()
    old = resolve_rack(**inputs)
    link = inputs["local_links"][0]
    replacement = replacement if replacement != getattr(link.ovs_port, field) else UUID(int=0)
    changed = dataclasses.replace(
        link, ovs_port=link.ovs_port.model_copy(update={field: replacement})
    )
    links = [changed, *inputs["local_links"][1:]]
    floor = authority.restrict(old, inputs["config"], inputs["inventory"], [], local_links=links)
    assert len(floor.sessions) == len(old.sessions) - 1
    assert link.interface not in {s.interface for s in floor.sessions}
    assert (
        authority.restrict(floor, inputs["config"], inputs["inventory"], [], local_links=links)
        == floor
    )
    assert (
        authority.restrict(old, inputs["config"], inputs["inventory"], [], local_links=None) == old
    )


@pytest.mark.parametrize("fault", ["port-uuid", "foreign-member", "uplink-bridge"])
def test_prestart_checks_native_uuid_and_kernel_ifindex_without_writes(tmp_path, fault):
    inputs = ovs_inputs()
    plan = resolve_rack(**inputs)
    # Restrict to one binding to keep the native table fixture narrowly scoped.
    binding = plan.sessions[0].ovs_port
    binding = binding.model_copy(
        update={
            "uplink": binding.uplink.model_copy(update={"bond": "bond0", "members": ("p0", "p1")})
        }
    )
    policy = external_policy.expected_policy(plan)
    policy["ovs_bindings"] = [binding.model_dump(mode="json")]
    host = ProvisionedHost(plan)
    host.service = object()
    replacement = binding.model_copy(update={"port_uuid": UUID(int=7)})
    active = binding
    pf = tmp_path / "devices/0000:03:00.0"
    pf.mkdir(parents=True)
    net = tmp_path / "class/net/p0"
    net.mkdir(parents=True)
    (net / "device").symlink_to(pf)
    bond = tmp_path / "class/net/bond0"
    (bond / "bonding").mkdir(parents=True)
    (bond / "bonding/mode").write_text("802.3ad 4\n")
    (bond / "bonding/slaves").write_text("p0 p1\n")
    (net / "master").symlink_to(bond)
    second = tmp_path / "class/net/p1"
    second.mkdir()
    (second / "device").symlink_to(pf)
    (second / "master").symlink_to(bond)
    bad_bridge = False

    def execute(args):
        if args[0] == "ovs-vsctl":
            documents = [json.loads(line) for line in snapshot_text(active).splitlines()]
            if bad_bridge:
                documents[0]["data"][0][2][1].pop()
            return subprocess.CompletedProcess(
                args, 0, "\n".join(json.dumps(document) for document in documents), ""
            )
        if args[0] == "devlink":
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    {"info": {"pci/0000:03:00.0": {"serial_number": binding.uplink.serial}}}
                ),
                "",
            )
        if args[:4] == ["ip", "-j", "link", "show"]:
            return subprocess.CompletedProcess(
                args, 0, json.dumps([{"ifindex": active.ifindex}]), ""
            )
        return host.execute(args)

    external_policy.validate(policy, execute, sysfs_root=tmp_path)
    if fault == "port-uuid":
        active = replacement
    elif fault == "foreign-member":
        foreign = tmp_path / "devices/0000:04:00.0"
        foreign.mkdir()
        (second / "device").unlink()
        (second / "device").symlink_to(foreign)
    else:
        bad_bridge = True
    with pytest.raises(external_policy.PolicyError, match="OVS internal"):
        external_policy.validate(policy, execute, sysfs_root=tmp_path)


@pytest.mark.parametrize("daemon_running", [True, False])
def test_recreated_port_commits_floor_before_old_binding_checks(
    rack_environment, monkeypatch, daemon_running
):
    """Exercise the real service path, including a stopped daemon after reboot.

    A name-reused port cannot be validated as the old UUID. The old generation
    must therefore never be a prerequisite for loading its own restriction.
    """
    _, _, _, service, host = rack_environment
    inputs = ovs_inputs()
    plan = resolve_rack(**inputs)
    passwords = {s.id: "test-only" for s in plan.sessions}
    present = {s.ovs_port for s in plan.sessions}

    def bindings(expected, execute, sysfs_root):
        if any(OvsPort.model_validate(item) not in present for item in expected):
            raise external_policy.PolicyError("Old OVS binding no longer exists")

    monkeypatch.setattr(external_policy, "_validate_ovs_bindings", bindings)
    service.apply_plan(plan, passwords)
    assert host.active
    lost = plan.sessions[0]
    present.remove(lost.ovs_port)
    links = [link for link in inputs["local_links"] if link.ovs_port in present]
    floor = authority.restrict(plan, inputs["config"], inputs["inventory"], [], local_links=links)
    host.active = daemon_running
    service.apply_plan(floor, passwords, restrictive=True)
    assert service.state() == floor
    assert lost.id not in {s.id for s in service.state().sessions}
    assert host.active
    service.verify_current()
    service.apply_plan(floor, passwords, restrictive=True)
    assert service.state() == floor
