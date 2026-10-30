"""Isolated Juju deployment smoke test; never enable routing on the test machines.

Run explicitly with pytest-jubilant's temporary-model fixture. This exercises packed
imports, principal placement, relation hooks, and expected missing-prerequisite states.
It does not assert BGP/BFD convergence or hardware offload.
"""

from pathlib import Path

import jubilant
import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration


def test_three_principals(juju: jubilant.Juju):
    for name in ["maas-inventory", "dpu-attachments", "bird"]:
        path = ROOT / "charms" / name / f"{name}_amd64.charm"
        assert path.exists(), f"Pack {name} first"
        juju.deploy(str(path), app=name)
    juju.integrate("maas-inventory:inventory", "bird:inventory")
    juju.integrate("dpu-attachments:attachments", "bird:attachments")
    status = juju.wait(
        lambda s: (
            jubilant.all_blocked(s, "maas-inventory", "bird")
            and jubilant.all_waiting(s, "dpu-attachments")
        ),
        timeout=600,
    )
    for name in ["maas-inventory", "dpu-attachments", "bird"]:
        assert len(status.apps[name].units) == 1
        assert all(unit.machine is not None for unit in status.apps[name].units.values())
