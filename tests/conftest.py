import importlib.util
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from maas_bird import external_policy, rack_runtime, runtime
from maas_bird.rack_runtime import RackService
from maas_bird.resolver import resolve_rack
from ops import testing

from tests.factories import make_inputs, rack_inputs, with_numa_contexts, with_routing_domains
from tests.rack_host_model import ProvisionedHost

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inputs():
    return make_inputs()


@pytest.fixture
def charm_context():
    def create(name):
        path = ROOT / "charms" / name
        spec = importlib.util.spec_from_file_location(
            name.replace("-", "_") + "_charm", path / "src/charm.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls = {
            "bird": "BirdCharm",
            "maas-inventory": "MaasInventoryCharm",
            "dpu-attachments": "DpuAttachmentsCharm",
        }[name]
        metadata = yaml.safe_load((path / "charmcraft.yaml").read_text())
        return testing.Context(getattr(module, cls), meta=metadata)

    return create


@pytest.fixture
def numa_inputs(inputs):
    return with_numa_contexts(inputs)


@pytest.fixture
def domain_inputs(numa_inputs):
    return with_routing_domains(numa_inputs)


@pytest.fixture
def rack_environment(tmp_path, monkeypatch):
    inputs = rack_inputs()
    plan = resolve_rack(**inputs)
    host = ProvisionedHost(plan)
    monkeypatch.setattr(external_policy, "host_uuid", lambda: "host-uuid")
    service = RackService(tmp_path, owner="model:bird/0")
    host.service = service
    monkeypatch.setattr(rack_runtime, "run", host.execute)
    monkeypatch.setattr(runtime, "run", host.execute)
    native_which = rack_runtime.shutil.which
    monkeypatch.setattr(
        rack_runtime.shutil,
        "which",
        lambda name, *args, **kwargs: (
            "/usr/sbin/bird" if name == "bird" else native_which(name, *args, **kwargs)
        ),
    )
    monkeypatch.setattr(runtime.os, "chown", lambda *args: None)
    monkeypatch.setattr(runtime.grp, "getgrnam", lambda _: Mock(gr_gid=os.getgid()))
    monkeypatch.setattr(service, "_control", host.control)
    monkeypatch.setattr(
        service,
        "health",
        lambda plan: {"up_sessions": [s.id for s in plan.sessions], "missing_routes": 0},
    )
    monkeypatch.setattr(rack_runtime.time, "monotonic", lambda: host.clock)
    monkeypatch.setattr(
        rack_runtime.time, "sleep", lambda seconds: setattr(host, "clock", host.clock + seconds)
    )
    passwords = {s.id: "test-only-password" for s in plan.sessions}
    return inputs, plan, passwords, service, host
