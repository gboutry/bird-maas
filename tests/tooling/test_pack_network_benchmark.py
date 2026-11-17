"""Extract the portable toolkit and exercise its real offline CLI without libraries."""

import hashlib
import json
import os
import runpy
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKER = ROOT / "scripts/pack_network_benchmark.py"


@pytest.fixture
def builder():
    return runpy.run_path(str(PACKER))


def test_deterministic_allowlisted_archive(builder, tmp_path):
    outputs = [tmp_path / "one.tar.gz", tmp_path / "two.tar.gz"]
    for output in outputs:
        builder["package"](output)
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    with tarfile.open(outputs[0]) as archive:
        members = archive.getmembers()
        assert all(
            member.isfile()
            and member.name.startswith("network-benchmark/")
            and ".." not in Path(member.name).parts
            for member in members
        )
        names = {member.name for member in members}
        assert names == {
            "network-benchmark/" + name
            for name in (
                *builder["FILES"],
                "README.md",
                "network_benchmark.py",
                "BUNDLE.json",
                "SHA256SUMS",
            )
        }
        archive.extractall(tmp_path / "extract", filter="data")
    extracted = tmp_path / "extract/network-benchmark"
    for line in (extracted / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert hashlib.sha256((extracted / name).read_bytes()).hexdigest() == digest
    with pytest.raises(FileExistsError):
        builder["package"](outputs[0])


def test_extracted_cli_needs_no_site_packages(builder, tmp_path):
    archive = tmp_path / "toolkit.tar.gz"
    builder["package"](archive)
    with tarfile.open(archive) as stream:
        stream.extractall(tmp_path, filter="data")
    extracted = tmp_path / "network-benchmark"
    # -I -S disables environment paths and site packages; trap SSH to prove offline validation.
    ssh = tmp_path / "ssh"
    marker = tmp_path / "ssh-was-invoked"
    ssh.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 99\n")
    ssh.chmod(0o755)
    environment = os.environ | {"PATH": str(tmp_path)}
    for arguments in (
        ("--help",),
        ("validate", "examples/network-benchmark/inventory.json"),
        ("plan", "examples/network-benchmark/manifest.json"),
    ):
        result = subprocess.run(
            [sys.executable, "-I", "-S", "network_benchmark.py", *arguments],
            cwd=extracted,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
    assert not marker.exists()
    result = subprocess.run(
        [sys.executable, "-I", "-S", "scripts/network_benchmark_ceph.py", "--help"],
        cwd=extracted,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0 and not marker.exists()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "network_benchmark.py",
            "capture",
            "examples/network-benchmark/inventory.json",
            "--output",
            "evidence",
        ],
        cwd=extracted,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0 and "--execute required" in result.stderr
    assert not marker.exists() and not (extracted / "evidence").exists()


def test_optional_renderer_is_bundled_without_installing(builder, tmp_path):
    renderer = tmp_path / "renderer"
    for name in builder["RENDERER_FILES"]:
        path = renderer / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture renderer: " + name)
    output = tmp_path / "with-renderer.tar.gz"
    builder["package"](output, renderer)
    with tarfile.open(output) as archive:
        for name in builder["RENDERER_FILES"]:
            file = archive.extractfile("network-benchmark/renderer/" + name)
            assert file is not None and file.read() == (renderer / name).read_bytes()
        file = archive.extractfile("network-benchmark/BUNDLE.json")
        assert file is not None and json.load(file)["renderer_included"]


def test_actual_builder_cli_refuses_missing_renderer_and_existing_archive(tmp_path):
    output = tmp_path / "bundle.tar.gz"
    result = subprocess.run(
        [
            sys.executable,
            str(PACKER),
            "--output",
            str(output),
            "--renderer-source",
            str(tmp_path / "missing"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0 and not output.exists()
    result = subprocess.run(
        [sys.executable, str(PACKER), "--output", str(output)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    before = output.read_bytes()
    result = subprocess.run(
        [sys.executable, str(PACKER), "--output", str(output)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0 and output.read_bytes() == before
