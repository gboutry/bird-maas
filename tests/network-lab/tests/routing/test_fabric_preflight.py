"""Reject malformed compiled FRR input through the native deployment CLI."""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "maas-bird-network-lab"
NODES = ("spine0", "spine1", "r1a", "r1b", "r2a", "r2b")
pytestmark = pytest.mark.integration


def fingerprints():
    return subprocess.check_output(
        [
            "lxc",
            "exec",
            "--project",
            PROJECT,
            "fabric",
            "--",
            "sha256sum",
            *(f"/opt/network-lab/host-fabric/frr/{node}.conf" for node in NODES),
        ],
        text=True,
        timeout=30,
    )


def test_native_parser_rejects_bad_input_before_live_replacement():
    before = fingerprints()
    with tempfile.TemporaryDirectory(
        prefix="fabric-preflight-", dir=ROOT / "runtime"
    ) as temporary:
        directory = Path(temporary) / "compiled"
        shutil.copytree(ROOT / "runtime/host-fabric", directory)
        config = directory / "frr/spine0.conf"
        marker = "network_lab_invalid_preflight_marker"
        config.write_text(config.read_text() + "\n" + marker + "\n")
        config.chmod(0o600)
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/apply_host_fabric.py"),
                "--directory",
                str(directory),
                "--apply",
            ],
            capture_output=True,
            text=True,
            timeout=240,
        )
        assert result.returncode != 0
        reports = [
            p
            for p in (ROOT / "runtime").glob("fabric-apply-[0-9]*.json")
            if json.loads(p.read_text())["source"] == str(directory.resolve())
        ]
        assert len(reports) == 1
        report = json.loads(reports[0].read_text())
        checks = [c for c in report["commands"] if "vtysh" in c["argv"] and "-C" in c["argv"]]
        assert checks
        marker_reported = any(marker in c["stdout"] + c["stderr"] for c in checks)
        assert marker_reported
        assert "applied_at" not in report
        assert fingerprints() == before
