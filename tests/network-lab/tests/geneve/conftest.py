"""Require real owned workers and the deployed OVN identity manifest."""

import importlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKERS = ("probe", "probe-alt", "probe-r2a", "probe-r2b")


@pytest.fixture
def overlay(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    deployment = importlib.import_module("deploy_ovn")
    identities = deployment.owned()
    manifest = json.loads((ROOT / "runtime/ovn/manifest.json").read_text())
    assert set(manifest) == set(WORKERS)
    assert all(manifest[name]["uuid"] == identities[name] for name in WORKERS)
    return deployment, manifest
