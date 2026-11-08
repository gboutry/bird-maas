"""Provider deployment waits for convergence without relaxing peer identity."""

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent / "scripts"))
    module = importlib.import_module("deploy_provider")
    elapsed = [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        module.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
    )
    return module


def summary(state="Established", address="10.0.0.1", asn=65001):
    return {"peers": {address: {"state": state, "remoteAs": asn}}}


def test_established_declared_peer_returns_native_summary(provider, monkeypatch):
    native = summary()
    monkeypatch.setattr(provider, "guest", lambda *args: json.dumps(native))
    assert provider.established_evpn_peers("spine0", {"10.0.0.1": "65001"}) == native


def test_initial_connect_state_is_observed_until_established(provider, monkeypatch):
    observations = iter((summary("Connect"), summary()))
    calls = []

    def guest(*args):
        calls.append(args)
        return json.dumps(next(observations))

    monkeypatch.setattr(provider, "guest", guest)
    assert provider.established_evpn_peers("spine0", {"10.0.0.1": "65001"}) == summary()
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0][2] == "clab-maas-bird-fabric-spine0"


@pytest.mark.parametrize(
    "native", (summary("Active"), summary(address="10.0.0.2"), summary(asn=65002))
)
def test_unready_or_foreign_peer_fails_at_bounded_deadline(provider, monkeypatch, native):
    monkeypatch.setattr(provider, "guest", lambda *args: json.dumps(native))
    with pytest.raises(RuntimeError, match="did not converge.*spine0"):
        provider.established_evpn_peers("spine0", {"10.0.0.1": "65001"}, timeout=4)
