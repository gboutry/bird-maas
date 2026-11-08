"""Deployment must reject foreign controllers and changed VM generations."""

import importlib
import json
import sys
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse

import pytest


@pytest.fixture
def deployment(monkeypatch, tmp_path):
    scripts = Path(__file__).resolve().parent / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    deploy = importlib.import_module("deploy")
    bootstrap = importlib.import_module("bootstrap")
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["deploy.py", "--apply", "--phase", "gateways"])
    events = []

    class Monitor:
        def __init__(self, *args):
            events.append("monitor-started")

        def poll(self):
            return None

        def terminate(self):
            events.append("monitor-stopped")

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(deploy.subprocess, "Popen", Monitor)
    monkeypatch.setattr(deploy, "gateways", lambda *_: events.append("gateway-action"))
    return deploy, bootstrap, events


class Instance(TypedDict):
    type: str
    config: dict[str, str]


def response(name) -> Instance:
    return {
        "type": "virtual-machine",
        "config": {
            "user.maas-bird-owner": "network-lab",
            "limits.cpu": "4" if name == "maas" else "2",
            "limits.memory": "8GiB" if name in {"maas", "fabric"} else "4GiB",
            "volatile.uuid": name + "-original",
        },
    }


@pytest.mark.parametrize("foreign", ("maas", "fabric", "juju"))
@pytest.mark.parametrize(
    "field,value",
    (
        ("user.maas-bird-owner", "foreign"),
        ("limits.cpu", "16"),
        ("limits.memory", "64GiB"),
    ),
)
def test_foreign_or_uncapped_controller_prevents_any_phase(
    deployment, monkeypatch, foreign, field, value
):
    deploy, bootstrap, events = deployment

    def query(*args):
        assert args[:2] == ("lxc", "query")
        name = urlparse(args[-1]).path.rsplit("/", 1)[1]
        row = response(name)
        if name == foreign:
            row["config"][field] = value
        return json.dumps(row)

    monkeypatch.setattr(bootstrap, "command", query)
    with pytest.raises(RuntimeError, match="owned, capped VM"):
        deploy.main()
    assert events == []


def test_replacement_after_preflight_prevents_phase_and_stops_monitor(deployment, monkeypatch):
    deploy, bootstrap, events = deployment
    calls = 0

    def query(*args):
        nonlocal calls
        calls += 1
        name = urlparse(args[-1]).path.rsplit("/", 1)[1]
        row = response(name)
        if calls > 7 and name == "fabric":
            row["config"]["volatile.uuid"] = "fabric-replacement"
        return json.dumps(row)

    monkeypatch.setattr(bootstrap, "command", query)
    with pytest.raises(RuntimeError, match="identities changed before deployment"):
        deploy.main()
    assert events == ["monitor-started", "monitor-stopped"]
