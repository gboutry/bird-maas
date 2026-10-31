"""Actual source routing, BGP/BFD failover and rollback in disposable Linux namespaces."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from maas_bird import runtime
from maas_bird.policy import PolicyError, PolicyRouting
from maas_bird.render import quoted, render
from maas_bird.resolver import resolve
from maas_bird.runtime import accepted

BIRD = Path(os.environ["BIRD_BINARY"]).resolve()
BIRDC = BIRD.with_name("birdc")
SINK = """import socket,struct,json,sys
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_IP,8,1)
s.bind(("0.0.0.0",19001))
with open(sys.argv[1],"a",buffering=1) as f:
 while True:
  data,anc,flags,peer=s.recvmsg(4096,128)
  iface=next(struct.unpack("I4s4s",v)[0] for l,t,v in anc if l==socket.SOL_IP and t==8)
  print(json.dumps({"token":data.decode(),"source":peer[0],"interface":socket.if_indextoname(iface)}),file=f)
"""


def wait(predicate, message, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(message)


class Lab:
    def __init__(self, directory):
        self.directory = directory
        self.runtime = Path("/run/maas-bird-tests")
        self.runtime.mkdir()
        self.processes = []
        self.created = []
        self.names = ["mb-host", "mb-tor0", "mb-tor1", "mb-dst", "mb-mgmt"]
        self.counter = 0

    def run(self, args, check=True):
        return self.ns("mb-host", args, check)

    def ns(self, name, args, check=True):
        return subprocess.run(
            ["ip", "netns", "exec", name, *map(str, args)],
            capture_output=True,
            text=True,
            timeout=10,
            check=check,
        )

    def ip(self, name, *args):
        return self.ns(name, ["ip", *args])

    def link(self, left, lname, right, rname):
        subprocess.run(
            ["ip", "link", "add", lname, "type", "veth", "peer", "name", rname], check=True
        )
        subprocess.run(["ip", "link", "set", lname, "netns", left], check=True)
        subprocess.run(["ip", "link", "set", rname, "netns", right], check=True)
        for ns, name in [(left, lname), (right, rname)]:
            self.ip(ns, "link", "set", "dev", name, "addrgenmode", "none")
            self.ip(ns, "link", "set", name, "up")
            self.ip(ns, "-6", "addr", "flush", "dev", name, "scope", "link")

    def setup(self):
        for name in self.names:
            subprocess.run(["ip", "netns", "add", name], check=True)
            self.created.append(name)
            self.ip(name, "link", "set", "lo", "up")
            for key in ["net.ipv4.ip_forward", "net.ipv6.conf.all.forwarding"]:
                self.ns(name, ["sysctl", "-q", "-w", f"{key}=1"])
            self.ns(name, ["sysctl", "-q", "-w", "net.ipv4.conf.all.rp_filter=0"])
            self.ns(name, ["sysctl", "-q", "-w", "net.ipv4.conf.default.rp_filter=0"])
        for n in range(2):
            self.link("mb-host", f"fabric{n + 1}", f"mb-tor{n}", f"up{n}")
            self.ip(
                "mb-host", "-6", "addr", "add", "fe80::11/64", "dev", f"fabric{n + 1}", "nodad"
            )
            self.ip(f"mb-tor{n}", "-6", "addr", "add", "fe80::1/64", "dev", f"up{n}", "nodad")
            self.link(f"mb-tor{n}", f"down{n}", "mb-dst", f"recv{n}")
            self.ip(f"mb-tor{n}", "addr", "add", f"192.0.{n + 2}.1/30", "dev", f"down{n}")
            self.ip("mb-dst", "addr", "add", f"192.0.{n + 2}.2/30", "dev", f"recv{n}")
            self.ip(f"mb-tor{n}", "route", "add", "203.0.113.0/24", "via", f"192.0.{n + 2}.2")
        self.link("mb-host", "management", "mb-mgmt", "managed")
        self.ip("mb-host", "addr", "add", "172.20.0.1/24", "dev", "management")
        self.ip("mb-mgmt", "addr", "add", "172.20.0.2/24", "dev", "managed")
        self.ip("mb-mgmt", "addr", "add", "172.21.0.2/32", "dev", "lo")
        # A deliberate main-table leak trap. Source policy must never send public traffic here.
        self.ip("mb-mgmt", "addr", "add", "203.0.113.9/32", "dev", "lo")
        self.ip("mb-host", "route", "add", "default", "via", "172.20.0.2", "dev", "management")
        self.ip(
            "mb-host", "route", "add", "172.21.0.0/24", "via", "172.20.0.2", "dev", "management"
        )
        self.ip("mb-dst", "addr", "add", "203.0.113.9/32", "dev", "lo")
        for vip in ["10.60.11.11", "10.60.11.12", "10.60.11.99"]:
            self.ip("mb-host", "addr", "add", vip + "/32", "dev", "lo")
        for name in ["mb-dst", "mb-mgmt"]:
            log = self.directory / (name + ".jsonl")
            self.processes.append(
                subprocess.Popen(
                    ["ip", "netns", "exec", name, sys.executable, "-u", "-c", SINK, str(log)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            )
        wait(
            lambda: all((self.directory / (n + ".jsonl")).exists() for n in ["mb-dst", "mb-mgmt"]),
            "UDP receivers did not start",
        )

    def bird(self, name, config, label):
        path = self.directory / (label + ".conf")
        path.write_text(config)
        log = open(self.directory / (label + ".log"), "w")
        process = subprocess.Popen(
            [
                "ip",
                "netns",
                "exec",
                name,
                str(BIRD),
                "-f",
                "-c",
                str(path),
                "-s",
                str(self.runtime / (label + ".ctl")),
                "-P",
                str(self.runtime / (label + ".pid")),
            ],
            stdout=log,
            stderr=log,
        )
        log.close()
        self.processes.append(process)
        return process

    def cli(self, label, *args):
        # AF_UNIX control sockets are filesystem objects; no network namespace switch needed.
        return subprocess.run(
            [str(BIRDC), "-s", str(self.runtime / (label + ".ctl")), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout

    def route(self, source, destination="203.0.113.9"):
        return self.ip("mb-host", "-j", "route", "get", destination, "from", source)

    def send(self, source, destination, receiver, interface):
        self.counter += 1
        token = f"packet-{self.counter}"
        program = (
            "import socket,sys;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
            "s.bind((sys.argv[1],0));s.sendto(sys.argv[3].encode(),(sys.argv[2],19001))"
        )
        self.ns("mb-host", [sys.executable, "-c", program, source, destination, token])
        log = self.directory / (receiver + ".jsonl")

        def received():
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            return any(
                row == {"token": token, "source": source, "interface": interface} for row in rows
            )

        wait(received, f"{token} did not arrive via {receiver}/{interface}", timeout=3)

    def fault(self, n, enabled):
        name = f"mb-tor{n}"
        if enabled:
            self.ns(name, ["nft", "add", "table", "inet", "fault"])
            self.ns(
                name,
                [
                    "nft",
                    "add",
                    "chain",
                    "inet",
                    "fault",
                    "ingress",
                    "{ type filter hook prerouting priority -300; policy accept; }",
                ],
            )
            self.ns(
                name,
                ["nft", "add", "rule", "inet", "fault", "ingress", "iifname", f"up{n}", "drop"],
            )
        else:
            self.ns(name, ["nft", "delete", "table", "inet", "fault"])

    def close(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            process.communicate(timeout=5)
        for name in reversed(self.created):
            subprocess.run(["ip", "netns", "del", name], capture_output=True, check=True)
        for label in ["host", "tor0", "tor1"]:
            for suffix in [".ctl", ".pid"]:
                (self.runtime / (label + suffix)).unlink(missing_ok=True)
        self.runtime.rmdir()


@pytest.fixture
def lab(tmp_path):
    assert os.environ.get("MAAS_BIRD_ISOLATED_TESTS") == "1", "Use scripts/test_namespaces.py"
    assert os.geteuid() == 0
    lab = Lab(tmp_path)
    try:
        lab.setup()
        yield lab
    finally:
        snapshots = {}
        for label in ["host", "tor0", "tor1"]:
            try:
                snapshots[label] = lab.cli(label, "show", "protocols", "all")
                snapshots[label + "_routes"] = lab.cli(
                    label, "show", "route", "table", "all", "all"
                )
            except subprocess.SubprocessError:
                snapshots[label] = "control unavailable"
        snapshots["kernel"] = lab.run(["ip", "-j", "-N", "route", "show", "table", "all"]).stdout
        (lab.directory / "diagnostics.json").write_text(json.dumps(snapshots, indent=2))
        lab.close()


def prepare_plan(numa_inputs, interval):
    numa_inputs["config"]["import-prefixes"] = "203.0.113.0/24"
    numa_inputs["config"]["bfd-min-tx-ms"] = interval
    numa_inputs["config"]["bfd-min-rx-ms"] = interval
    return resolve(**numa_inputs)


def tor_config(n, interval):
    return f"""router id 10.255.0.{n + 1};
log stderr all;
protocol device {{}}
protocol kernel main_kernel {{ learn; ipv4 {{ import all; export all; }}; }}
protocol bfd fabric {{ interface "up{n}" {{ min tx interval {interval} ms;
 min rx interval {interval} ms; multiplier 3; }}; }}
protocol bgp host {{
 local fe80::1 as {65011 + n}; neighbor fe80::11%up{n} as 4200001011;
 interface "up{n}"; direct; strict bind yes; bfd yes;
 password "example";
 connect retry time 2; error wait time 1, 5;
 hold time 9; keepalive time 3;
 ipv4 {{ extended next hop on; import all;
 export filter {{ if net=203.0.113.0/24 then accept; reject; }}; }};
}}
"""


@pytest.mark.parametrize("interval", [50, 300])
def test_numa_forwarding_bfd_failover_and_fail_closed(lab, numa_inputs, interval):
    plan = prepare_plan(numa_inputs, interval)
    policy = PolicyRouting(lab.directory / "policy", "namespace-test", lab.run)
    baseline = lab.run(["ip", "-j", "-N", "rule", "show"]).stdout
    policy.ensure(plan)
    lab.bird(
        "mb-host",
        "log stderr all;\n" + render(plan, {"path1": "example", "path2": "example"}),
        "host",
    )
    for n in range(2):
        lab.bird(f"mb-tor{n}", tor_config(n, interval), f"tor{n}")

    def dev(source):
        result = lab.ns(
            "mb-host", ["ip", "-j", "route", "get", "203.0.113.9", "from", source], check=False
        )
        if result.returncode:
            return None
        return json.loads(result.stdout)[0].get("dev")

    wait(
        lambda: dev("10.60.11.11") == "fabric1" and dev("10.60.11.12") == "fabric2",
        "Independent healthy local paths did not install",
    )
    assert policy.verify_routes(plan)
    lab.send("10.60.11.11", "203.0.113.9", "mb-dst", "recv0")
    lab.send("10.60.11.12", "203.0.113.9", "mb-dst", "recv1")
    # Prove the leak trap and management/replication paths are actually usable.
    lab.send("10.60.11.99", "203.0.113.9", "mb-mgmt", "managed")
    lab.send("10.60.11.11", "172.20.0.2", "mb-mgmt", "managed")
    lab.send("10.60.11.12", "172.21.0.2", "mb-mgmt", "managed")
    for n, source, backup in [(0, "10.60.11.11", "fabric2"), (1, "10.60.11.12", "fabric1")]:
        lab.fault(n, True)  # Interfaces remain UP; only BFD can promptly detect this blackhole.
        wait(
            lambda source=source, backup=backup: dev(source) == backup,
            "BFD did not withdraw the silent failed path",
        )
        assert "Up" in lab.cli("host", "show", "bfd", "sessions")
        assert (
            "bfd session down"
            in lab.cli("host", "show", "protocols", "all", f"uplink_{n}").lower()
        )
        lab.send(source, "203.0.113.9", "mb-dst", f"recv{1 - n}")
        lab.fault(n, False)
        wait(
            lambda: dev("10.60.11.11") == "fabric1" and dev("10.60.11.12") == "fabric2",
            "NUMA locality did not restore after path recovery",
        )
    lab.fault(0, True)
    lab.fault(1, True)
    wait(
        lambda: dev("10.60.11.11") is None and dev("10.60.11.12") is None,
        "Both-path failure leaked through main instead of unreachable guards",
    )
    for index, source in enumerate(["10.60.11.11", "10.60.11.12"]):
        result = lab.ns(
            "mb-host", ["ip", "route", "get", "203.0.113.9", "from", source], check=False
        )
        assert result.returncode != 0
        # Keep semantic route type names for this independently observed kernel assertion.
        guards = json.loads(lab.run(["ip", "-j", "route", "show", "table", "all"]).stdout)
        assert any(
            route.get("type") == "unreachable"
            and route.get("dst") == "default"
            and str(route.get("table")) == str(3100 + index)
            and int(route.get("protocol", 0)) == 196
            and route.get("metric") == 42760
            for route in guards
        )
        blocked = lab.ns(
            "mb-host",
            [
                sys.executable,
                "-c",
                "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
                f"s.bind(('{source}',0));s.sendto(b'must-not-leak',('203.0.113.9',19001))",
            ],
            check=False,
        )
        assert blocked.returncode != 0
    lab.send("10.60.11.11", "172.20.0.2", "mb-mgmt", "managed")
    lab.send("10.60.11.12", "172.21.0.2", "mb-mgmt", "managed")
    # Removing only journaled objects restores the exact original RPDB, not a flush.
    for process in lab.processes[2:]:
        process.terminate()
        process.wait(timeout=5)
    policy.remove()
    assert lab.run(["ip", "-j", "-N", "rule", "show"]).stdout == baseline


def snapshot(lab):
    return (
        json.loads(lab.run(["ip", "-j", "-N", "rule", "show"]).stdout),
        json.loads(lab.run(["ip", "-j", "-N", "route", "show", "table", "all"]).stdout),
    )


def test_partial_policy_install_restores_original_state(lab, numa_inputs):
    plan = prepare_plan(numa_inputs, 50)
    lab.ip(
        "mb-host",
        "rule",
        "add",
        "priority",
        "8000",
        "from",
        "198.18.0.0/24",
        "to",
        "198.19.0.0/24",
        "table",
        "7000",
        "protocol",
        "197",
    )
    baseline = snapshot(lab)
    mutations = 0

    def fail_second_rule(args):
        nonlocal mutations
        if "add" in args:
            mutations += 1
            if mutations == 4:
                raise RuntimeError("injected second-rule failure")
        return lab.run(args)

    policy = PolicyRouting(lab.directory / "policy", "test-owner", fail_second_rule)
    with pytest.raises(RuntimeError, match="injected"):
        policy.ensure(plan)
    assert snapshot(lab) == baseline
    assert not policy.path.exists()


@pytest.mark.parametrize("collision", ["table", "priority", "selectors"])
def test_policy_collisions_do_not_mutate_existing_state(lab, numa_inputs, collision):
    plan = prepare_plan(numa_inputs, 50)
    if collision == "table":
        lab.ip(
            "mb-host",
            "route",
            "add",
            "blackhole",
            "198.51.100.1/32",
            "table",
            "3100",
            "proto",
            "197",
        )
    else:
        lab.ip(
            "mb-host",
            "rule",
            "add",
            "priority",
            "10000" if collision == "priority" else "7000",
            "from",
            "192.0.2.0/24" if collision == "priority" else "10.60.11.11/32",
            "to",
            "203.0.113.0/24",
            "table",
            "7000",
            "protocol",
            "197",
        )
    baseline = snapshot(lab)
    policy = PolicyRouting(lab.directory / "policy", "test-owner", lab.run)
    with pytest.raises(PolicyError):
        policy.ensure(plan)
    assert snapshot(lab) == baseline
    assert not policy.path.exists()


def test_unacknowledged_kernel_operation_is_not_adopted_or_deleted(lab, numa_inputs):
    plan = prepare_plan(numa_inputs, 50)

    def lose_ack(args):
        result = lab.run(args)
        if "add" in args:
            raise RuntimeError("injected lost acknowledgement")
        return result

    policy = PolicyRouting(lab.directory / "policy", "test-owner", lose_ack)
    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        policy.ensure(plan)
    before = snapshot(lab)
    policy.execute = lab.run
    with pytest.raises(PolicyError, match="inspection"):
        policy.ensure(plan)
    with pytest.raises(PolicyError, match="automatically"):
        policy.remove()
    assert snapshot(lab) == before
    assert policy.path.exists()


def test_immutable_layout_and_foreign_replacement_are_protected(lab, numa_inputs):
    plan = prepare_plan(numa_inputs, 50)
    policy = PolicyRouting(lab.directory / "policy", "test-owner", lab.run)
    policy.ensure(plan)
    baseline = snapshot(lab)
    changed = plan.model_copy(
        update={
            "contexts": (plan.contexts[0].model_copy(update={"table": 3200}), plan.contexts[1])
        }
    )
    with pytest.raises(PolicyError, match="immutable"):
        policy.ensure(changed)
    assert snapshot(lab) == baseline
    lab.ip(
        "mb-host",
        "rule",
        "del",
        "priority",
        "10000",
        "from",
        "10.60.11.11/32",
        "to",
        "203.0.113.0/24",
        "table",
        "3100",
        "protocol",
        "196",
    )
    lab.ip(
        "mb-host",
        "rule",
        "add",
        "priority",
        "10000",
        "from",
        "192.0.2.0/24",
        "to",
        "203.0.113.0/24",
        "table",
        "7000",
        "protocol",
        "197",
    )
    foreign = snapshot(lab)
    with pytest.raises(PolicyError, match="Foreign rule"):
        policy.remove()
    assert snapshot(lab) == foreign


def test_runtime_initial_failure_rolls_back_policy(lab, numa_inputs, monkeypatch):

    plan = prepare_plan(numa_inputs, 50)
    baseline = snapshot(lab)
    service = runtime.BirdService(lab.directory)
    service.directory.mkdir(parents=True)
    monkeypatch.setattr(service, "_prepare", lambda _: None)
    monkeypatch.setattr(runtime, "run", lab.run)

    def fail(*args, **kwargs):
        raise runtime.RuntimeErrorBase("injected parser failure")

    monkeypatch.setattr(service, "apply", fail)
    with pytest.raises(runtime.RuntimeErrorBase, match="parser"):
        service.apply_contexts(render(plan, {"path1": "one", "path2": "two"}), plan)
    assert snapshot(lab) == baseline
    assert not (service.directory / "policy.json").exists()


def test_unconfirmed_context_candidate_restores_local_routes(lab, numa_inputs):
    plan = prepare_plan(numa_inputs, 50)
    policy = PolicyRouting(lab.directory / "policy", "test-owner", lab.run)
    policy.ensure(plan)
    lab.bird("mb-host", render(plan, {"path1": "example", "path2": "example"}), "host")
    for n in range(2):
        lab.bird(f"mb-tor{n}", tor_config(n, 50), f"tor{n}")
    wait(lambda: policy.verify_routes(plan), "Local context routes did not establish")
    rule_snapshot = lab.run(["ip", "-j", "-N", "rule", "show"]).stdout
    journal = policy.path.read_bytes()
    swapped = plan.model_copy(
        update={
            "contexts": tuple(
                c.model_copy(update={"numa_node": 1 - c.numa_node}) for c in plan.contexts
            )
        }
    )
    candidate = lab.directory / "unconfirmed.conf"
    candidate.write_text(render(swapped, {"path1": "example", "path2": "example"}))
    lab.cli("host", "configure", quoted(str(candidate)), "timeout", "3")
    wait(
        lambda: policy.verify_routes(swapped), "Candidate preferences did not reach the kernel", 2
    )
    wait(
        lambda: policy.verify_routes(plan), "Native rollback did not restore NUMA-local routes", 8
    )
    assert lab.run(["ip", "-j", "-N", "rule", "show"]).stdout == rule_snapshot
    assert policy.path.read_bytes() == journal
    lab.send("10.60.11.11", "203.0.113.9", "mb-dst", "recv0")
    lab.send("10.60.11.12", "203.0.113.9", "mb-dst", "recv1")


def test_reconciliation_detects_new_overlapping_foreign_policy(lab, numa_inputs):
    plan = prepare_plan(numa_inputs, 50)
    policy = PolicyRouting(lab.directory / "policy", "test-owner", lab.run)
    policy.ensure(plan)
    baseline = snapshot(lab)
    policy.ensure(plan)
    assert snapshot(lab) == baseline
    lab.ip(
        "mb-host",
        "rule",
        "add",
        "priority",
        "7000",
        "from",
        "10.60.11.11/32",
        "to",
        "203.0.113.0/24",
        "table",
        "7000",
        "protocol",
        "197",
    )
    foreign = snapshot(lab)
    with pytest.raises(PolicyError, match="overlaps"):
        policy.ensure(plan)
    assert snapshot(lab) == foreign


def prepare_domains(lab, domain_inputs):
    """Add a second L2 adjacency on each synthetic TOR; no IPv4 transit addresses."""
    lab.names.append("mb-rdst")
    subprocess.run(["ip", "netns", "add", "mb-rdst"], check=True)
    lab.created.append("mb-rdst")
    lab.ip("mb-rdst", "link", "set", "lo", "up")
    for key in ["all", "default"]:
        lab.ns("mb-rdst", ["sysctl", "-q", "-w", f"net.ipv4.conf.{key}.rp_filter=0"])
    for n in range(2):
        lab.link("mb-host", f"replica{n + 1}", f"mb-tor{n}", f"repup{n}")
        lab.ip("mb-host", "-6", "addr", "add", "fe80::11/64", "dev", f"replica{n + 1}", "nodad")
        lab.ip(f"mb-tor{n}", "-6", "addr", "add", "fe80::1/64", "dev", f"repup{n}", "nodad")
        lab.link(f"mb-tor{n}", f"rdown{n}", "mb-rdst", f"recv{n}")
        lab.ip(f"mb-tor{n}", "addr", "add", f"192.0.{n + 4}.1/30", "dev", f"rdown{n}")
        lab.ip("mb-rdst", "addr", "add", f"192.0.{n + 4}.2/30", "dev", f"recv{n}")
        lab.ip(f"mb-tor{n}", "route", "add", "198.51.100.0/24", "via", f"192.0.{n + 4}.2")
        lab.ip("mb-host", "addr", "add", f"172.22.0.{11 + n}/32", "dev", "lo")
    lab.ip("mb-rdst", "addr", "add", "198.51.100.9/32", "dev", "lo")
    lab.ip("mb-mgmt", "addr", "add", "198.51.100.9/32", "dev", "lo")
    log = lab.directory / "mb-rdst.jsonl"
    lab.processes.append(
        subprocess.Popen(
            ["ip", "netns", "exec", "mb-rdst", sys.executable, "-u", "-c", SINK, str(log)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    )
    wait(log.exists, "Replica receiver did not start")
    return resolve(**domain_inputs)


def start_domains(lab, plan):
    lab.bird("mb-host", render(plan, {s.id: "example" for s in plan.sessions}), "host")
    for n in range(2):
        config = f"""router id 10.255.0.{n + 1};
log stderr all;
protocol device {{}}
protocol kernel main_kernel {{ learn; ipv4 {{ import all; export all; }}; }}
protocol bfd fabric {{
 interface "up{n}" {{ min tx interval 50 ms; min rx interval 50 ms; multiplier 3; }};
 interface "repup{n}" {{ min tx interval 300 ms; min rx interval 300 ms; multiplier 3; }};
}}
"""
        for name, interface in [("public", f"up{n}"), ("replica", f"repup{n}")]:
            # Deliberately offer BOTH domains' routes on each session. Host filters
            # must reject the foreign domain even when the trusted TOR offers it.
            config += f"""protocol bgp {name} {{
 local fe80::1 as {65011 + n}; neighbor fe80::11%{interface} as 4200001011;
 interface "{interface}"; direct; strict bind yes; bfd yes; password "example";
 connect retry time 2; error wait time 1, 5; hold time 9; keepalive time 3;
 ipv4 {{ extended next hop on; import all;
  export filter {{ if net ~ [203.0.113.0/24,198.51.100.0/24] then accept; reject; }}; }};
}}
"""
        lab.bird(f"mb-tor{n}", config, f"tor{n}")


def domain_dev(lab, source, destination):
    result = lab.ns(
        "mb-host", ["ip", "-j", "route", "get", destination, "from", source], check=False
    )
    return json.loads(result.stdout)[0].get("dev") if result.returncode == 0 else None


def replica_fault(lab, n, enabled):
    name = f"mb-tor{n}"
    if not enabled:
        lab.ns(name, ["nft", "delete", "table", "inet", "replica_fault"])
        return
    lab.ns(name, ["nft", "add", "table", "inet", "replica_fault"])
    lab.ns(
        name,
        [
            "nft",
            "add",
            "chain",
            "inet",
            "replica_fault",
            "ingress",
            "{ type filter hook prerouting priority -300; policy accept; }",
        ],
    )
    lab.ns(
        name,
        ["nft", "add", "rule", "inet", "replica_fault", "ingress", "iifname", f"repup{n}", "drop"],
    )


def blocked_datagram(lab, source, destination):
    result = lab.ns(
        "mb-host",
        [
            sys.executable,
            "-c",
            "import socket,sys;"
            "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind((sys.argv[1],0));"
            "s.sendto(b'must-not-leak',(sys.argv[2],19001))",
            source,
            destination,
        ],
        check=False,
    )
    assert result.returncode != 0


def test_public_replica_domains_forward_fail_over_and_never_leak(lab, domain_inputs):

    plan = prepare_domains(lab, domain_inputs)
    baseline = snapshot(lab)
    policy = PolicyRouting(lab.directory / "policy", "multi-owner", lab.run)
    policy.ensure(plan)
    start_domains(lab, plan)
    wait(
        lambda: policy.verify_routes(plan), "Independent public/replica contexts did not establish"
    )
    assert accepted(
        lab.cli("host", "show", "protocols", "all"), lab.cli("host", "show", "bfd", "sessions"), 4
    )
    bfd = lab.cli("host", "show", "bfd", "sessions")
    (lab.directory / "bfd-sessions.txt").write_text(bfd)
    timers = {
        row[1]: (float(row[-2]), float(row[-1]))
        for line in bfd.splitlines()
        if (row := line.split()) and row[0].startswith("fe80:")
    }
    assert timers == {
        "fabric1": (0.05, 0.15),
        "fabric2": (0.05, 0.15),
        "replica1": (0.3, 0.9),
        "replica2": (0.3, 0.9),
    }
    for n in range(2):
        public, replica = f"10.60.11.{11 + n}", f"172.22.0.{11 + n}"
        lab.send(public, "203.0.113.9", "mb-dst", f"recv{n}")
        lab.send(replica, "198.51.100.9", "mb-rdst", f"recv{n}")
        blocked_datagram(lab, public, "198.51.100.9")
        blocked_datagram(lab, replica, "203.0.113.9")
        for index in range(2):
            public_routes = lab.cli("host", "show", "route", "table", f"d0_path_{index}", "all")
            replica_routes = lab.cli(
                "host", "show", "route", "table", f"d1_path_{index + 2}", "all"
            )
            assert "198.51.100.0/24" not in public_routes
            assert "203.0.113.0/24" not in replica_routes
    # Verify exported VIPs are separated too, despite one common TOR routing table.
    for session, forbidden in [("public", "172.22.0."), ("replica", "10.60.11.")]:
        routes = lab.cli("tor0", "show", "route", "protocol", session, "all")
        assert forbidden not in routes
    lab.fault(0, True)
    wait(
        lambda: domain_dev(lab, "10.60.11.11", "203.0.113.9") == "fabric2",
        "Public BFD failover failed",
    )
    lab.send("10.60.11.11", "203.0.113.9", "mb-dst", "recv1")
    lab.send("172.22.0.11", "198.51.100.9", "mb-rdst", "recv0")
    lab.fault(1, True)
    wait(
        lambda: domain_dev(lab, "10.60.11.11", "203.0.113.9") is None,
        "Public traffic leaked through main",
    )
    blocked_datagram(lab, "10.60.11.11", "203.0.113.9")
    lab.send("172.22.0.12", "198.51.100.9", "mb-rdst", "recv1")
    for n in range(2):
        lab.fault(n, False)
    wait(lambda: policy.verify_routes(plan), "Public recovery failed")
    replica_fault(lab, 0, True)
    wait(
        lambda: domain_dev(lab, "172.22.0.11", "198.51.100.9") == "replica2",
        "Replica BFD failover failed",
    )
    lab.send("172.22.0.11", "198.51.100.9", "mb-rdst", "recv1")
    replica_fault(lab, 1, True)
    wait(
        lambda: domain_dev(lab, "172.22.0.11", "198.51.100.9") is None,
        "Replica traffic leaked through main",
    )
    blocked_datagram(lab, "172.22.0.11", "198.51.100.9")
    lab.send("10.60.11.11", "203.0.113.9", "mb-dst", "recv0")
    lab.send("10.60.11.11", "172.20.0.2", "mb-mgmt", "managed")
    lab.send("172.22.0.11", "172.21.0.2", "mb-mgmt", "managed")
    for n in range(2):
        replica_fault(lab, n, False)
    wait(lambda: policy.verify_routes(plan), "Replica recovery failed")
    # A foreign cross-domain route must prevent confirmation of a new candidate.
    lab.ip(
        "mb-host",
        "route",
        "add",
        "198.51.100.0/24",
        "dev",
        "management",
        "table",
        "3100",
        "proto",
        "197",
    )
    assert not policy.verify_routes(plan)
    lab.ip(
        "mb-host",
        "route",
        "del",
        "198.51.100.0/24",
        "dev",
        "management",
        "table",
        "3100",
        "proto",
        "197",
    )
    for process in lab.processes:
        process.terminate()
        process.wait(timeout=5)
    policy.remove()
    assert snapshot(lab) == baseline


def test_multiple_domain_policy_rolls_back_whole_install(lab, domain_inputs):
    plan = prepare_domains(lab, domain_inputs)
    baseline = snapshot(lab)
    (lab.directory / "baseline.json").write_text(json.dumps(baseline))
    count = 0

    def fail_replica_rule(args):
        nonlocal count
        if "add" in args:
            count += 1
            if count == 10:
                raise RuntimeError("injected replica-domain rule failure")
        return lab.run(args)

    policy = PolicyRouting(lab.directory / "policy", "multi-owner", fail_replica_rule)
    with pytest.raises(RuntimeError, match="replica-domain"):
        policy.ensure(plan)
    (lab.directory / "after-failure.json").write_text(json.dumps(snapshot(lab)))
    assert snapshot(lab) == baseline
    assert not policy.path.exists()
    policy.execute = lab.run
    policy.ensure(plan)
    before = snapshot(lab)
    renamed = plan.model_copy(
        update={
            "domains": (plan.domains[0], plan.domains[1].model_copy(update={"domain": "renamed"}))
        }
    )
    with pytest.raises(PolicyError, match="immutable"):
        policy.ensure(renamed)
    assert snapshot(lab) == before
    policy.remove()
    assert snapshot(lab) == baseline


def test_unconfirmed_replica_candidate_rolls_back_all_domains(lab, domain_inputs):
    plan = prepare_domains(lab, domain_inputs)
    policy = PolicyRouting(lab.directory / "policy", "multi-owner", lab.run)
    policy.ensure(plan)
    start_domains(lab, plan)
    wait(lambda: policy.verify_routes(plan), "Multi-domain routes did not establish")
    journal = policy.path.read_bytes()
    rules = snapshot(lab)[0]
    replica = plan.domains[1]
    swapped = replica.model_copy(
        update={
            "contexts": tuple(
                c.model_copy(update={"numa_node": 1 - c.numa_node}) for c in replica.contexts
            )
        }
    )
    candidate_plan = plan.model_copy(update={"domains": (plan.domains[0], swapped)})
    candidate = lab.directory / "unconfirmed-domains.conf"
    candidate.write_text(render(candidate_plan, {s.id: "example" for s in plan.sessions}))
    lab.cli("host", "configure", quoted(str(candidate)), "timeout", "3")
    wait(lambda: policy.verify_routes(candidate_plan), "Replica candidate did not install", 2)
    lab.send("10.60.11.11", "203.0.113.9", "mb-dst", "recv0")
    lab.send("172.22.0.11", "198.51.100.9", "mb-rdst", "recv1")
    wait(lambda: policy.verify_routes(plan), "Timed rollback did not restore both domains", 8)
    assert snapshot(lab)[0] == rules
    assert policy.path.read_bytes() == journal
    lab.send("172.22.0.11", "198.51.100.9", "mb-rdst", "recv0")
