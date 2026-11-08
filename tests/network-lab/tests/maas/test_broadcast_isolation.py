"""Actual tagged broadcasts must remain inside their fabric and VLAN domain."""

import importlib
import json
import subprocess
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

import pytest
from maas_bird.runtime import write_atomic

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration


def test_reused_role_vids_do_not_extend_broadcasts_across_fabrics_or_vlans(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    lab = importlib.import_module("deploy_ovn")
    recipe = importlib.import_module("generate_roles")
    provision = importlib.import_module("deploy_probe")
    commission = importlib.import_module("commission")
    identities = lab.owned()
    names = tuple(lab.WORKERS)
    interfaces = [
        f"{parent}.{role['vlan']}" for parent in ("enp7s0", "enp8s0") for role in recipe.roles()
    ]
    source = (ROOT / "maas/compatibility-tests/broadcast_probe.py").read_text()
    token = uuid.uuid4().hex
    directory = ROOT / f"runtime/broadcast-isolation-{time.time_ns()}"
    directory.mkdir(mode=0o700)
    receipt = {
        "started": time.time(),
        "worker_uuids": {n: identities[n] for n in names},
        "nonce": token,
        "interfaces": interfaces,
    }
    scopes = {}
    for name in names:
        runtime = ROOT / "runtime" if name == "probe" else ROOT / "runtime" / name
        enrollment = json.loads((runtime / "commission.json").read_text())
        machine = commission.maas("machine", "read", enrollment["system_id"])
        assert machine["status_name"] == "Deployed" and machine["hostname"] == name
        assert machine["hardware_uuid"] == identities[name] == enrollment["lxd_uuid"]
        provision.validate_configuration(
            machine, name, "loopback32", lab.HOSTS[name]["management_ip"]
        )
        provision.validate_roles(machine)
        children = {i["name"]: i for i in machine["interface_set"]}
        for parent in ("enp7s0", "enp8s0"):
            fabrics = {
                children[f"{parent}.{r['vlan']}"]["vlan"]["fabric_id"] for r in recipe.roles()
            }
            assert len(fabrics) == 1
            fabric = fabrics.pop()
            domain = f"rack{lab.HOSTS[name]['rack']}/{parent}"
            assert scopes.setdefault(domain, fabric) == fabric
    assert len(scopes) == len(set(scopes.values())) == 4
    receipt["maas_fabric_domains"] = scopes
    processes = {}
    cases = {
        name: [{"id": name + ":" + interface, "interface": interface} for interface in interfaces]
        for name in ("probe", "probe-r2a")
    }

    def argv(name, mode, configuration):
        return [
            "lxc",
            "exec",
            "--project",
            lab.PROJECT,
            name,
            "--",
            "python3",
            "-u",
            "-c",
            source,
            mode,
            "--token",
            token,
            "--configuration",
            json.dumps(configuration),
        ]

    with ExitStack() as files:
        try:
            for name in names:
                (directory / (name + ".jsonl")).touch(mode=0o600)
                (directory / (name + ".stderr")).touch(mode=0o600)
                output = files.enter_context((directory / (name + ".jsonl")).open("w"))
                errors = files.enter_context((directory / (name + ".stderr")).open("w"))
                processes[name] = subprocess.Popen(
                    argv(name, "receive", interfaces), stdout=output, stderr=errors, text=True
                )
            deadline = time.monotonic() + 10
            while True:
                ready = {}
                for name in names:
                    lines = (directory / (name + ".jsonl")).read_text().splitlines()
                    if lines:
                        ready[name] = json.loads(lines[0])
                if len(ready) == 4:
                    assert all(r["ready"] == interfaces for r in ready.values())
                    break
                assert all(p.poll() is None for p in processes.values()), "Native capture exited"
                assert time.monotonic() < deadline, "Native capture did not become ready"
                time.sleep(0.1)
            receipt["ready"] = time.time()
            sent = {}
            for name, rows in cases.items():
                completed = subprocess.run(
                    argv(name, "send", rows),
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                )
                sent[name] = json.loads(completed.stdout)
                assert len(sent[name]["sent"]) == 30
            receipt["sent"] = sent
            received = {}
            for name, process in processes.items():
                process.communicate(timeout=40)
                assert process.returncode == 0, (directory / (name + ".stderr")).read_text()
                lines = (directory / (name + ".jsonl")).read_text().splitlines()
                assert len(lines) == 2
                received[name] = json.loads(lines[1])["received"]
                assert set(received[name]) == set(interfaces)
            receipt["received"] = received
            expected = {}
            for sender, rows in cases.items():
                peer = next(
                    n
                    for n in names
                    if n != sender and lab.HOSTS[n]["rack"] == lab.HOSTS[sender]["rack"]
                )
                for row in rows:
                    expected[row["id"]] = {"worker": peer, "interface": row["interface"]}
            assert len(expected) == 20
            receipt["expected"] = expected
            for name in names:
                for interface in interfaces:
                    wanted = {
                        key: [0, 1, 2]
                        for key, value in expected.items()
                        if value == {"worker": name, "interface": interface}
                    }
                    actual = {
                        key: sorted(sequences)
                        for key, sequences in received[name][interface].items()
                    }
                    assert actual == wanted, {
                        "worker": name,
                        "interface": interface,
                        "expected": wanted,
                        "received": actual,
                    }
            receipt["passed"] = True
        finally:
            for process in processes.values():
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=20)
            receipt["completed"] = time.time()
            write_atomic(directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600)
