"""Real source-bound storage routing; no Ceph cluster or hardware-locality claim."""

import importlib
import json
import re
import subprocess
import time
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import jubilant
import pytest
from maas_bird.models import MultiDomainPlan
from maas_bird.render import protocol_name

ROOT = Path(__file__).resolve().parents[2]
HOSTS = json.loads((ROOT / "maas/network-model/hosts.json").read_text())
pytestmark = pytest.mark.integration


def members(config):
    names = config.getoption("storage_members").split(",")
    if len(names) != len(set(names)) or len(names) < 2 or not set(names) <= set(HOSTS):
        raise pytest.UsageError(
            "Storage acceptance requires at least two distinct enrolled members"
        )
    if len({HOSTS[name]["rack"] for name in names}) != 2:
        raise pytest.UsageError("Storage acceptance must span both racks")
    return tuple(name for name in HOSTS if name in names)


def pytest_generate_tests(metafunc):
    if "name" in metafunc.fixturenames:
        metafunc.parametrize("name", members(metafunc.config))


@pytest.fixture(scope="module")
def storage_network(request):
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(ROOT / "scripts"))
        lab = importlib.import_module("deploy_ovn")
        recipe = importlib.import_module("generate_roles")
        workers = importlib.import_module("juju_workers")
        mapping = importlib.import_module("render_host_fabric").WORKERS
        cohort = members(request.config)
        juju = jubilant.Juju(model="network-lab:network-lab")
        apps = [prefix + mapping[name] for name in cohort for prefix in ("bird-", "nic-")]
        juju.wait(lambda s: jubilant.all_active(s, *apps), timeout=240)
        bindings = {
            name: workers.worker_binding(juju, name, "bird-" + mapping[name]) for name in cohort
        }
        yield lab, recipe, juju, bindings, cohort


def save(name, kind, data):
    runtime = importlib.import_module("maas_bird.runtime")
    directory = ROOT / "runtime/ceph"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime.write_atomic(
        directory / f"{name}-{kind}.json", json.dumps(data, indent=2) + "\n", 0o600
    )


@pytest.mark.parametrize("transport", ("tcp", "udp"))
@pytest.mark.parametrize("slot", range(4))
def test_bounded_storage_application_traffic(storage_network, name, slot, transport):
    """Exercise actual sockets on each storage role, without a Ceph benchmark."""
    lab, recipe, _, bindings, cohort = storage_network
    target = next(n for n in cohort if HOSTS[n]["rack"] != HOSTS[name]["rack"])
    role = recipe.roles()[slot]
    assert role["domain"] in {"ceph-access", "ceph-replication"}
    source_ip = recipe.address(name, role).split("/")[0]
    target_ip = recipe.address(target, role).split("/")[0]
    for node in (name, target):
        assert (
            lab.ssh(node, "sudo", "cat", "/sys/class/dmi/id/product_uuid") == bindings[node].uuid
        )
    port = str(54000 + slot)
    assert not lab.ssh(target, "ss", "-lntH", f"sport = :{port}")
    server = subprocess.Popen(
        lab.ssh_args(
            target, "timeout", "20", "iperf3", "-s", "-1", "-J", "-B", target_ip, "-p", port
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    receipt = {
        "source": name,
        "target": target,
        "role": role,
        "transport": transport,
        "source_ip": source_ip,
        "target_ip": target_ip,
        "bitrate_limit": 128000,
        "duration_seconds": 3,
    }
    try:
        deadline = time.monotonic() + 10
        while not lab.ssh(target, "ss", "-lntH", f"sport = :{port}"):
            assert server.poll() is None and time.monotonic() < deadline
            time.sleep(0.1)
        args = [
            "iperf3",
            "-c",
            target_ip,
            "-B",
            source_ip,
            "-p",
            port,
            "-t",
            "3",
            "-b",
            "128K",
            "-l",
            "1200",
            "-J",
            "--connect-timeout",
            "3000",
        ]
        if transport == "udp":
            args.append("-u")
        client = json.loads(lab.ssh(name, *args, timeout=15))
        stdout, stderr = server.communicate(timeout=15)
        receiver = json.loads(stdout)
        receipt.update(client=client, receiver=receiver, server_stderr=stderr)
        assert server.returncode == 0 and "error" not in receiver and "error" not in client
        connected = client["start"]["connected"]
        assert len(connected) == 1
        assert connected[0]["local_host"] == source_ip
        assert connected[0]["remote_host"] == target_ip
        assert client["end"]["sum_sent"]["bytes"] > 0
        received = receiver["end"]["sum_received"]
        assert received["bytes"] > 0 and received["seconds"] >= 2.5
        assert received["bytes"] <= 64000
        if transport == "udp":
            streams = receiver["end"]["streams"]
            assert len(streams) == 1
            statistics = streams[0]["udp"]
            assert statistics["packets"] > 0
            assert statistics["lost_packets"] == 0
            assert statistics["out_of_order"] == 0
        receipt["passed"] = True
    finally:
        if server.poll() is None:
            server.terminate()
            server.communicate(timeout=25)
        save(name, f"application-{slot}-{transport}", receipt)


def test_all_role_sessions_tables_and_guards(storage_network, name):
    lab, recipe, juju, bindings, cohort = storage_network
    plan = MultiDomainPlan.model_validate_json(
        juju.run(bindings[name].unit, "plan").results["plan"]
    )
    assert plan.host.system_uuid == bindings[name].uuid
    assert {d.domain for d in plan.domains} == {"general", "ceph-access", "ceph-replication"}
    assert sum(len(d.contexts) for d in plan.domains) == 5
    allocation = recipe.roles()
    expected = {
        protocol_name(
            role["domain"],
            f"{name}-{'gp' if role['id'] == 'general' else role['id']}-"
            f"r{HOSTS[name]['rack']}{side}",
        )
        for role in allocation
        for side in ("a", "b")
    }
    protocols = lab.ssh(name, "sudo", "birdc", "show", "protocols")
    rows = [row.split() for row in protocols.splitlines()]
    bgp = {r[0]: r for r in rows if len(r) >= 6 and r[1] == "BGP"}
    assert len(expected) == 10 and set(bgp) == expected
    assert all(r[3] == "up" and "Established" in r[5:] for r in bgp.values())
    bfd = lab.ssh(name, "sudo", "birdc", "show", "bfd", "sessions")
    sessions = [row.split() for row in bfd.splitlines() if row.strip().startswith("fe80:")]
    interfaces = {f"{parent}.{r['vlan']}" for r in allocation for parent in ("enp7s0", "enp8s0")}
    assert len(sessions) == 10 and {r[1] for r in sessions} == interfaces
    assert all(r[0] == "fe80::1" and r[2] == "Up" for r in sessions)
    assert all(
        Decimal(r[-2]) == Decimal("0.300") and Decimal(r[-1]) == Decimal("0.900") for r in sessions
    )
    rules = json.loads(lab.ssh(name, "ip", "-j", "-4", "-N", "rule", "show"))
    tables = {}
    for role in allocation:
        selected = [r for r in rules if r["priority"] == role["rule_priority"]]
        assert len(selected) == 1
        assert selected[0]["src"].removesuffix("/32") == recipe.address(name, role).split("/")[0]
        assert (
            str(selected[0]["table"]) == str(role["table"]) and str(selected[0]["protocol"]) == "4"
        )
        routes = json.loads(
            lab.ssh(name, "ip", "-j", "-4", "-N", "route", "show", "table", str(role["table"]))
        )
        tables[str(role["table"])] = routes
        learned = {
            r["dst"].removesuffix("/32"): r for r in routes if str(r.get("protocol")) == "12"
        }
        peers = HOSTS if role["domain"] == "general" else cohort
        same_domain = [r for r in allocation if r["domain"] == role["domain"]]
        destinations = {
            recipe.address(other, r).split("/")[0]
            for other in peers
            if other != name
            for r in same_domain
        }
        expected_paths = (
            importlib.import_module("routing_expectations").general_paths(name)
            if role["domain"] == "general"
            else {ip: {f"enp7s0.{role['vlan']}", f"enp8s0.{role['vlan']}"} for ip in destinations}
        )
        assert set(learned) == set(expected_paths)
        for address, route in learned.items():
            hops = route.get("nexthops", [route])
            assert len(hops) == len(expected_paths[address])
            assert {h["dev"] for h in hops} == expected_paths[address]
            assert all(
                h["via"]["family"] == "inet6" and h["via"]["host"] == "fe80::1" for h in hops
            )
        guards = [r for r in routes if r.get("dst") == "default"]
        assert len(guards) == 1 and str(guards[0]["type"]) == "7"
        assert str(guards[0]["protocol"]) == "4" and guards[0]["metric"] == 42760
    filtering = lab.ssh(
        name,
        "cat",
        "/proc/sys/net/ipv4/conf/all/rp_filter",
        *(f"/proc/sys/net/ipv4/conf/{interface}/rp_filter" for interface in sorted(interfaces)),
    )
    assert filtering.split() == ["2"] * 11
    save(
        name,
        "control",
        {
            "members": cohort,
            "uuid": bindings[name].uuid,
            "plan": plan.model_dump(mode="json"),
            "protocols": protocols,
            "bfd": bfd,
            "rules": rules,
            "tables": tables,
        },
    )


def routed_packets(storage_network, name, *, include_general=False):
    lab, recipe, _, _, cohort = storage_network
    records = []
    for role in recipe.roles():
        if role["domain"] == "general" and not include_general:
            continue
        source = recipe.address(name, role).split("/")[0]
        for other in HOSTS if role["domain"] == "general" else cohort:
            if other == name:
                continue
            for target in recipe.roles():
                if target["domain"] != role["domain"]:
                    continue
                destination = recipe.address(other, target).split("/")[0]
                route = json.loads(
                    lab.ssh(
                        name, "ip", "-j", "-4", "-N", "route", "get", destination, "from", source
                    )
                )[0]
                assert str(route["table"]) == str(role["table"])
                assert route["dev"] in {f"enp7s0.{role['vlan']}", f"enp8s0.{role['vlan']}"}
                packets = lab.ssh(
                    name,
                    "ping",
                    "-n",
                    "-M",
                    "do",
                    "-s",
                    "1472",
                    "-I",
                    source,
                    "-c",
                    "3",
                    "-i",
                    "0.2",
                    "-W",
                    "1",
                    destination,
                )
                assert "3 packets transmitted, 3 received, 0% packet loss" in packets
                records.append(
                    {
                        "source": source,
                        "destination": destination,
                        "route": route,
                        "packets": packets,
                    }
                )
    return records


def test_each_storage_source_reaches_every_same_domain_remote_at_full_mtu(storage_network, name):
    records = routed_packets(storage_network, name)
    save(name, "packets", {"members": storage_network[-1], "paths": records})


def test_managed_sources_reject_other_domains_and_management_routes(storage_network, name):
    lab, recipe, _, _, cohort = storage_network
    other = next(n for n in cohort if n != name)
    records = []
    for role in recipe.roles():
        source = recipe.address(name, role).split("/")[0]
        targets = [
            recipe.address(other, r).split("/")[0]
            for r in recipe.roles()
            if r["domain"] != role["domain"]
        ]
        for destination in (*targets, "10.203.240.10"):
            result = json.loads(
                lab.ssh(
                    name,
                    "python3",
                    "-c",
                    "import json,subprocess,sys; "
                    "p=subprocess.run(['ip','-4','route','get',sys.argv[1],'from',sys.argv[2]],"
                    "capture_output=True,text=True); "
                    "print(json.dumps({'rc':p.returncode,'stdout':p.stdout,'stderr':p.stderr}))",
                    destination,
                    source,
                )
            )
            assert result["rc"] == 2
            records.append({"source": source, "destination": destination, **result})
    control = lab.ssh(name, "ping", "-n", "-c", "2", "-W", "1", "10.203.240.10")
    assert "2 packets transmitted, 2 received, 0% packet loss" in control
    save(
        name,
        "negative-routes",
        {"members": cohort, "routes": records, "management_control": control},
    )


def wait_for(check, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return check()
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def paths_and_bfd(network, name, parent=None, gp_parent=None):
    lab, recipe, _, _, cohort = network
    tables = {}
    for role in recipe.roles():
        routes = json.loads(
            lab.ssh(name, "ip", "-j", "-4", "-N", "route", "show", "table", str(role["table"]))
        )
        tables[str(role["table"])] = routes
        learned = {
            r["dst"].removesuffix("/32"): r for r in routes if str(r.get("protocol")) == "12"
        }
        expected = {
            recipe.address(other, r).split("/")[0]
            for other in (HOSTS if role["domain"] == "general" else cohort)
            if other != name
            for r in recipe.roles()
            if r["domain"] == role["domain"]
        }
        devices = {f"{p}.{role['vlan']}" for p in ((parent,) if parent else ("enp7s0", "enp8s0"))}
        expected_paths = (
            importlib.import_module("routing_expectations").general_paths(
                name,
                parents=(gp_parent or parent,) if gp_parent or parent else ("enp7s0", "enp8s0"),
            )
            if role["domain"] == "general"
            else {ip: devices for ip in expected}
        )
        assert set(learned) == set(expected_paths)
        for address, route in learned.items():
            hops = route.get("nexthops", [route])
            assert len(hops) == len(expected_paths[address])
            assert {h["dev"] for h in hops} == expected_paths[address]
        guards = [r for r in routes if r.get("dst") == "default"]
        assert len(guards) == 1 and str(guards[0]["type"]) == "7"
        assert str(guards[0]["protocol"]) == "4" and guards[0]["metric"] == 42760
    bfd = lab.ssh(name, "sudo", "birdc", "show", "bfd", "sessions")
    up = {
        r.split()[1]
        for r in bfd.splitlines()
        if r.strip().startswith("fe80:") and r.split()[2] == "Up"
    }
    expected = {
        f"{p}.{r['vlan']}"
        for r in recipe.roles()
        for p in (
            (gp_parent,)
            if gp_parent and r["domain"] == "general"
            else ((parent,) if parent else ("enp7s0", "enp8s0"))
        )
    }
    assert up == expected
    return {"tables": tables, "bfd": bfd}


def loopbacks(lab, name):
    addresses = json.loads(lab.ssh(name, "ip", "-j", "-4", "address", "show", "dev", "lo"))
    return {a["local"] for a in addresses[0]["addr_info"] if a["prefixlen"] == 32}


def link_change(lab, name, parent, state):
    return json.loads(
        lab.command(
            "lxc",
            "exec",
            "--project",
            lab.PROJECT,
            name,
            "--",
            "python3",
            "-c",
            "import json,subprocess,sys,time; a=time.time(); m=time.monotonic(); "
            "subprocess.run(['ip','link','set','dev',sys.argv[1],sys.argv[2]],check=True); "
            "print(json.dumps({'before_wall':a,'after_wall':time.time(),"
            "'before_monotonic':m,'after_monotonic':time.monotonic()}))",
            parent,
            state,
        )
    )


@contextmanager
def failed_parent(lab, name, parent, receipt):
    try:
        receipt["down"] = link_change(lab, name, parent, "down")
        yield
    finally:
        receipt["up"] = link_change(lab, name, parent, "up")


def reply_times(text, destination):
    return [
        float(t)
        for t in re.findall(rf"(?m)^\[(\d+\.\d+)\].* bytes from {re.escape(destination)}:", text)
    ]


def test_both_fast_parents_fail_closed_while_management_remains_available(storage_network, name):
    lab, recipe, juju, bindings, cohort = storage_network
    paths_and_bfd(storage_network, name)
    assert lab.ssh(name, "sudo", "cat", "/sys/class/dmi/id/product_uuid") == bindings[name].uuid
    before = loopbacks(lab, name)
    other = next(n for n in cohort if HOSTS[n]["rack"] != HOSTS[name]["rack"])
    receipt: dict[str, Any] = {"members": cohort, "uuid": bindings[name].uuid}
    directory = ROOT / "runtime/ceph" / f"{name}-both-down-{time.time_ns()}"
    directory.mkdir(mode=0o700, parents=True)
    capture_path, diagnostics_path = directory / "capture.txt", directory / "capture.err"
    capture_path.touch(mode=0o600)
    diagnostics_path.touch(mode=0o600)
    with capture_path.open("w") as output, diagnostics_path.open("w") as errors:
        capture = subprocess.Popen(
            lab.ssh_args(
                name,
                "sudo",
                "timeout",
                "--signal=INT",
                "--kill-after=5",
                "35",
                "tcpdump",
                "--immediate-mode",
                "-n",
                "-l",
                "-tt",
                "-Q",
                "out",
                "-i",
                "bond0",
                "icmp",
            ),
            stdout=output,
            stderr=errors,
            text=True,
        )
        try:

            def capture_ready():
                assert capture.poll() is None
                assert "listening on bond0" in diagnostics_path.read_text()

            wait_for(capture_ready, 10)
            with ExitStack() as faults:
                for parent in ("enp7s0", "enp8s0"):
                    event = {}
                    receipt[parent] = event
                    faults.enter_context(failed_parent(lab, name, parent, event))

                def withdrawn():
                    tables = {}
                    for role in recipe.roles():
                        rows = json.loads(
                            lab.ssh(
                                name,
                                "ip",
                                "-j",
                                "-4",
                                "-N",
                                "route",
                                "show",
                                "table",
                                str(role["table"]),
                            )
                        )
                        assert not any(str(r.get("protocol")) == "12" for r in rows)
                        guards = [r for r in rows if r.get("dst") == "default"]
                        assert len(guards) == 1 and str(guards[0]["type"]) == "7"
                        assert str(guards[0]["protocol"]) == "4" and guards[0]["metric"] == 42760
                        tables[str(role["table"])] = rows
                    return tables

                receipt["withdrawn_tables"] = wait_for(withdrawn, 15)
                assert loopbacks(lab, name) == before
                packets = []
                for role in recipe.roles():
                    source = recipe.address(name, role).split("/")[0]
                    destination = recipe.address(other, role).split("/")[0]
                    result = subprocess.run(
                        lab.ssh_args(
                            name, "ping", "-n", "-I", source, "-c", "1", "-W", "1", destination
                        ),
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    assert result.returncode != 0 and "0 received" in result.stdout
                    packets.append(
                        {
                            "source": source,
                            "destination": destination,
                            "returncode": result.returncode,
                            "stdout": result.stdout,
                            "stderr": result.stderr,
                        }
                    )
                receipt["unreachable_packets"] = packets
                control = lab.ssh(name, "ping", "-n", "-c", "2", "-W", "1", "10.203.240.10")
                assert "2 packets transmitted, 2 received, 0% packet loss" in control
                receipt["management_packets"] = control
                assert (
                    juju.exec(
                        "sudo", "cat", "/sys/class/dmi/id/product_uuid", unit=bindings[name].unit
                    ).stdout.strip()
                    == bindings[name].uuid
                )
                capture.communicate(timeout=40)
                assert capture.returncode == 124
                text = capture_path.read_text()
                diagnostics = diagnostics_path.read_text()
                assert "0 packets dropped by kernel" in diagnostics
                assert len(re.findall(r" > 10\.203\.240\.10: ICMP echo request", text)) == 2
                assert not re.search(r" IP 10\.205\.", text)
                receipt["management_capture"] = {"stdout": text, "stderr": diagnostics}
            receipt["fault_assertions_passed"] = True
        finally:
            if capture.poll() is None:
                capture.terminate()
                capture.communicate(timeout=40)
            try:
                recovery_started = time.monotonic()
                try:
                    receipt["restored"] = wait_for(
                        lambda: paths_and_bfd(storage_network, name), 30
                    )
                except AssertionError as error:
                    receipt["strict_restoration_failure"] = str(error)
                    receipt["strict_restoration_elapsed"] = time.monotonic() - recovery_started
                    # Preserve the failed gate while restoring the shared lab
                    # before another fault case can start.
                    receipt["eventual_restored"] = wait_for(
                        lambda: paths_and_bfd(storage_network, name), 180
                    )
                    receipt["eventual_restoration_elapsed"] = time.monotonic() - recovery_started
                    raise
                assert loopbacks(lab, name) == before
                receipt["passed"] = receipt.get("fault_assertions_passed", False)
            finally:
                save(name, "both-parents-down", receipt)


@pytest.mark.parametrize(("failed", "surviving"), (("enp7s0", "enp8s0"), ("enp8s0", "enp7s0")))
def test_one_fast_parent_removes_five_children_and_preserves_live_flows(
    storage_network, name, failed, surviving
):
    lab, recipe, juju, bindings, cohort = storage_network
    workers = importlib.import_module("juju_workers")
    mapping = importlib.import_module("render_host_fabric").WORKERS
    assert workers.worker_binding(juju, name, "bird-" + mapping[name]).uuid == bindings[name].uuid
    paths_and_bfd(storage_network, name)
    before = loopbacks(lab, name)
    assert before == {recipe.address(name, r).split("/")[0] for r in recipe.roles()}
    links = {r["ifname"]: r for r in json.loads(lab.ssh(name, "ip", "-j", "link", "show"))}
    interfaces = [f"{p}.{r['vlan']}" for p in (failed, surviving) for r in recipe.roles()]
    assert all("LOWER_UP" in links[i]["flags"] for i in interfaces)
    directory = ROOT / "runtime/ceph" / f"{name}-{failed}-{time.time_ns()}"
    directory.mkdir(parents=True, mode=0o700)
    destination_host = next(n for n in cohort if n != name)
    flows: list[dict[str, Any]] = [
        {
            "role": r,
            "id": 46000 + i,
            "source": recipe.address(name, r).split("/")[0],
            "destination": recipe.address(destination_host, r).split("/")[0],
        }
        for i, r in enumerate(recipe.roles())
    ]
    indices = " or ".join(f"ifindex {links[i]['ifindex']}" for i in interfaces)
    identifiers = " or ".join(f"icmp[4:2] = {f['id']}" for f in flows)
    sources = " or ".join(f"src host {f['source']}" for f in flows)
    capture_filter = f"({indices}) and icmp and icmp[0] = 8 and ({identifiers}) and ({sources})"
    processes = []
    receipt: dict[str, Any] = {
        "members": cohort,
        "uuid": bindings[name].uuid,
        "failed": failed,
        "surviving": surviving,
        "loopbacks_before": sorted(before),
        "flows": flows,
    }
    with ExitStack() as files:

        def output(filename):
            path = directory / filename
            path.touch(mode=0o600)
            return files.enter_context(path.open("w"))

        capture = subprocess.Popen(
            lab.ssh_args(
                name,
                "sudo",
                "timeout",
                "--signal=INT",
                "--kill-after=5",
                "45",
                "tcpdump",
                "--immediate-mode",
                "-n",
                "-l",
                "-tt",
                "-Q",
                "out",
                "-i",
                "any",
                capture_filter,
            ),
            stdout=output("capture.txt"),
            stderr=output("capture.err"),
            text=True,
        )
        processes.append(capture)
        try:

            def capture_ready():
                assert capture.poll() is None
                assert "listening on any" in (directory / "capture.err").read_text()

            wait_for(capture_ready, 10)
            for flow in flows:
                process = subprocess.Popen(
                    lab.ssh_args(
                        name,
                        "stdbuf",
                        "-oL",
                        "ping",
                        "-n",
                        "-D",
                        "-O",
                        "-M",
                        "do",
                        "-s",
                        "1472",
                        "-I",
                        flow["source"],
                        "-e",
                        str(flow["id"]),
                        "-i",
                        "0.1",
                        "-w",
                        "45",
                        "-W",
                        "1",
                        flow["destination"],
                    ),
                    stdout=output(f"{flow['id']}.txt"),
                    stderr=output(f"{flow['id']}.err"),
                    text=True,
                )
                processes.append(process)

            def baseline_ready():
                assert all(p.poll() is None for p in processes)
                assert all(
                    reply_times((directory / f"{f['id']}.txt").read_text(), f["destination"])
                    for f in flows
                )

            wait_for(baseline_ready, 10)
            started = time.monotonic()
            with failed_parent(lab, name, failed, receipt):
                changed = {
                    r["ifname"]: r for r in json.loads(lab.ssh(name, "ip", "-j", "link", "show"))
                }
                assert all(
                    "LOWER_UP" not in changed[f"{failed}.{r['vlan']}"]["flags"]
                    for r in recipe.roles()
                )
                assert all(
                    "LOWER_UP" in changed[f"{surviving}.{r['vlan']}"]["flags"]
                    for r in recipe.roles()
                )
                assert loopbacks(lab, name) == before
                receipt["during"] = wait_for(
                    lambda: paths_and_bfd(storage_network, name, surviving), 30
                )
                receipt["route_ready_observer_seconds"] = time.monotonic() - started
                ready_wall = float(
                    lab.ssh(name, "python3", "-c", "import time; print(time.time())")
                )
                receipt["route_ready_guest_wall"] = ready_wall

                def flows_recovered():
                    assert all(
                        any(
                            t > ready_wall
                            for t in reply_times(
                                (directory / f"{f['id']}.txt").read_text(), f["destination"]
                            )
                        )
                        for f in flows
                    )

                wait_for(flows_recovered, 15)
                receipt["data_ready_observer_seconds"] = time.monotonic() - started
                receipt["managed_packets_during_fault"] = routed_packets(
                    storage_network, name, include_general=True
                )
                control = lab.ssh(name, "ping", "-n", "-c", "2", "-W", "1", "10.203.240.10")
                assert "2 packets transmitted, 2 received, 0% packet loss" in control
                receipt["management_during_fault"] = control
                print(
                    f"{name}: {failed} down; all five roles recovered through {surviving}",
                    flush=True,
                )
                for process in processes:
                    process.wait(timeout=55)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            receipt["restored"] = wait_for(lambda: paths_and_bfd(storage_network, name), 150)
            assert loopbacks(lab, name) == before
            save(name, "parent-" + failed, receipt)
    assert capture.returncode == 124
    capture_text = (directory / "capture.txt").read_text()
    events: list[dict[str, Any]] = []
    for line in capture_text.strip().splitlines():
        match = re.fullmatch(
            r"(\d+\.\d+)\s+(\S+)\s+Out\s+IP\s+([\d.]+)\s+>\s+([\d.]+): "
            r"ICMP echo request, id (\d+), seq (\d+), length \d+",
            line,
        )
        assert match is not None, line
        events.append(
            {
                "time": float(match[1]),
                "interface": match[2],
                "source": match[3],
                "destination": match[4],
                "id": int(match[5]),
                "seq": int(match[6]),
            }
        )
    assert {e["id"] for e in events} == {f["id"] for f in flows}
    errors = (directory / "capture.err").read_text()
    assert re.search(r"\b0 packets dropped by kernel\b", errors)
    for flow, process in zip(flows, processes[1:], strict=True):
        assert process.returncode == 0
        text = (directory / f"{flow['id']}.txt").read_text()
        times = reply_times(text, flow["destination"])
        assert any(t < receipt["down"]["before_wall"] for t in times)
        assert any(t > receipt["route_ready_guest_wall"] for t in times)
        assert max(times) > receipt["up"]["before_wall"] - 2
        stats = re.search(r"(\d+) packets transmitted, (\d+) received", text)
        assert stats is not None
        after = {
            e["interface"]
            for e in events
            if e["id"] == flow["id"] and e["time"] > receipt["route_ready_guest_wall"]
        }
        assert after == {f"{surviving}.{flow['role']['vlan']}"}
        initial = {
            e["interface"]
            for e in events
            if e["id"] == flow["id"] and e["time"] < receipt["down"]["before_wall"]
        }
        assert initial
        flow.update(
            transmitted=int(stats[1]),
            received=int(stats[2]),
            max_inter_reply_gap_seconds=max(b - a for a, b in pairwise(times)),
            reply_trace=text,
            initial_interfaces=sorted(initial),
            fault_interfaces=sorted(after),
            initially_used_failed_parent=any(i.startswith(failed + ".") for i in initial),
        )
    receipt.update(passed=True, captured_egress=events, capture_stderr=errors)
    save(name, "parent-" + failed, receipt)
    print(f"{name}: {failed} restored; ten BFD sessions and both paths recovered", flush=True)


@pytest.mark.parametrize("failed_parent", ("enp7s0", "enp8s0"))
def test_one_gp_child_preserves_storage_and_other_gp_path(storage_network, name, failed_parent):
    lab, _, _, _, _ = storage_network
    survivor = "enp8s0" if failed_parent == "enp7s0" else "enp7s0"
    interface = failed_parent + ".3404"
    receipt = {"failed_child": interface, "started": time.time()}
    receipt["before"] = paths_and_bfd(storage_network, name)
    before_loopbacks = loopbacks(lab, name)
    try:
        lab.ssh(name, "sudo", "ip", "link", "set", "dev", interface, "down")
        receipt["fault_applied"] = time.time()
        receipt["surviving"] = wait_for(
            lambda: paths_and_bfd(storage_network, name, gp_parent=survivor), timeout=30
        )
        receipt["converged"] = time.time()
        receipt["packets"] = routed_packets(storage_network, name, include_general=True)
        expectations = importlib.import_module("routing_expectations")
        source = HOSTS[name]["loopback"].split("/")[0]
        receipt["gp_packets"] = {}
        for destination in expectations.general_paths(name):
            text = lab.ssh(
                name,
                "ping",
                "-n",
                "-M",
                "do",
                "-s",
                "1472",
                "-I",
                source,
                "-c",
                "3",
                "-i",
                "0.2",
                "-W",
                "1",
                destination,
            )
            assert "3 packets transmitted, 3 received, 0% packet loss" in text
            receipt["gp_packets"][destination] = text
        assert loopbacks(lab, name) == before_loopbacks
        parent = json.loads(lab.ssh(name, "ip", "-j", "link", "show", "dev", failed_parent))[0]
        assert "UP" in parent["flags"]
        receipt["fault_assertions_passed"] = True
    finally:
        try:
            lab.ssh(name, "sudo", "ip", "link", "set", "dev", interface, "up")
            recovery_started = time.monotonic()
            try:
                receipt["restored"] = wait_for(
                    lambda: paths_and_bfd(storage_network, name), timeout=30
                )
            except AssertionError as error:
                receipt["strict_restoration_failure"] = str(error)
                receipt["strict_restoration_elapsed"] = time.monotonic() - recovery_started
                receipt["eventual_restored"] = wait_for(
                    lambda: paths_and_bfd(storage_network, name), timeout=180
                )
                receipt["eventual_restoration_elapsed"] = time.monotonic() - recovery_started
                raise
            receipt["restored_packets"] = routed_packets(
                storage_network, name, include_general=True
            )
            receipt["passed"] = receipt.get("fault_assertions_passed", False)
        finally:
            save(name, "gp-child-" + failed_parent, receipt)
