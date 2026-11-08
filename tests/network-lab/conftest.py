"""Expose the lab's native orchestration helpers during explicit acceptance."""

from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--storage-members",
        default="probe,probe-alt,probe-r2a,probe-r2b",
        help="Explicit comma-separated storage cohort; "
        "final acceptance defaults to all four workers",
    )


@pytest.fixture(autouse=True)
def lab_scripts(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "scripts"))
