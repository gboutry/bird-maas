"""Empty native Juju registries permit fresh bootstrap and cleanup checks."""

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def bootstrap(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent / "scripts"))
    return importlib.import_module("bootstrap")


@pytest.mark.parametrize("controllers", [None, {}, {"network-lab": {"uuid": "known"}}])
def test_controller_registry_normalizes_only_native_empty_value(
    bootstrap, monkeypatch, controllers
):
    monkeypatch.setattr(bootstrap, "command", lambda *_: json.dumps({"controllers": controllers}))
    assert bootstrap.registered_controllers() == ({} if controllers is None else controllers)


@pytest.mark.parametrize("controllers", [[], ""])
def test_malformed_controller_registry_is_not_treated_as_empty(
    bootstrap, monkeypatch, controllers
):
    monkeypatch.setattr(bootstrap, "command", lambda *_: json.dumps({"controllers": controllers}))
    with pytest.raises(RuntimeError, match="not an object or null"):
        bootstrap.registered_controllers()


def test_fresh_bootstrap_creates_controller_from_native_null_registry(bootstrap, monkeypatch):
    created = []
    monkeypatch.setattr(bootstrap, "owned", lambda *_: "native-vm")

    def run(*args, **kwargs):
        if args[:2] == (str(bootstrap.JUJU), "bootstrap"):
            created.append(args)

    def command(*args):
        if args[1] == "controllers":
            return json.dumps({"controllers": None})
        assert args[1] == "show-controller"
        assert len(created) == 1
        return json.dumps(
            {
                "network-lab": {
                    "details": {
                        "cloud": "manual",
                        "api-endpoints": ["10.203.240.12:17070"],
                        "agent-version": "3.6.29",
                        "uuid": "new-controller",
                    }
                }
            }
        )

    monkeypatch.setattr(bootstrap, "run", run)
    monkeypatch.setattr(bootstrap, "command", command)
    assert bootstrap.juju_bootstrap() == {
        "vm_uuid": "native-vm",
        "controller_uuid": "new-controller",
        "agent_version": "3.6.29",
    }
