import importlib.util
from pathlib import Path

import pytest
import yaml
from ops import testing

from tests.factories import make_inputs, with_numa_contexts, with_routing_domains

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
