"""A seed must not start before host LLDP forwarding and cloud-init readiness."""

import importlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def seed(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent / "scripts"))
    module = importlib.import_module("bootstrap_fabric")
    monkeypatch.setattr(module, "owned", lambda *_: "owned-fabric")
    return module


def test_missing_lldp_prerequisite_refuses_before_guest_command(seed, monkeypatch):
    calls = []
    monkeypatch.setattr(seed, "Path", lambda _: SimpleNamespace(read_text=lambda: "0"))
    monkeypatch.setattr(seed, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(RuntimeError, match="Host LLDP forwarding prerequisite is missing"):
        seed.prerequisites()
    assert calls == []


def test_cloud_init_failure_is_not_accepted_as_seed_readiness(seed, monkeypatch):
    monkeypatch.setattr(seed, "Path", lambda _: SimpleNamespace(read_text=lambda: "0x4000"))

    def failed(*args, **kwargs):
        assert args[-3:] == ("cloud-init", "status", "--wait")
        raise subprocess.CalledProcessError(2, args)

    monkeypatch.setattr(seed, "run", failed)
    with pytest.raises(subprocess.CalledProcessError) as error:
        seed.prerequisites()
    assert error.value.returncode == 2
