"""Unit boundaries for provider source pins, build caps and mirror publication."""

import json
import runpy
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def builder(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return runpy.run_path(str(ROOT / "scripts/build_network_lab_provider.py"))


def elf():
    return b"\x7fELF\x02\x01" + bytes(12) + b"\x3e\x00" + bytes(44)


def test_patch_matches_immutable_provenance(builder, tmp_path):
    source, patch = builder["provenance"]()
    assert source["commit"] == "0d1ed5d0958fb7c9f60916fbee56688965271a54"
    assert source["version"] == "2.10.0-networklab.1"
    assert source["go_version"] == "go1.27.3"
    assert source["patch_sha256"] == builder["sha256"](patch)
    (tmp_path / "source.json").write_text(json.dumps(source))
    (tmp_path / "lifecycle.patch").write_bytes(patch.read_bytes() + b"corruption")
    with pytest.raises(ValueError, match="SHA256"):
        builder["provenance"](tmp_path)


def test_build_caps_override_environment(builder, monkeypatch):
    monkeypatch.setenv("GOMAXPROCS", "99")
    monkeypatch.setenv("GOFLAGS", "-p=99")
    environment = builder["build_environment"]()
    assert environment["GOMAXPROCS"] == "2"
    assert environment["GOFLAGS"] == (
        "-mod=readonly -p=2 -trimpath -buildvcs=false -ldflags=-buildid="
    )
    assert {key: environment[key] for key in ("GOOS", "GOARCH", "CGO_ENABLED")} == {
        "GOOS": "linux",
        "GOARCH": "amd64",
        "CGO_ENABLED": "0",
    }
    assert environment["GOTOOLCHAIN"] == "local"


def test_archive_is_deterministic_and_has_one_executable(builder, tmp_path):
    binary = tmp_path / "binary"
    binary.write_bytes(elf())
    archives = [tmp_path / "one.zip", tmp_path / "two.zip"]
    for archive in archives:
        builder["package"](binary, archive, "2.10.0-networklab.1")
    assert archives[0].read_bytes() == archives[1].read_bytes()
    with zipfile.ZipFile(archives[0]) as archive:
        assert archive.namelist() == ["terraform-provider-maas_v2.10.0-networklab.1"]
        entry = archive.infolist()[0]
        assert entry.date_time == (1980, 1, 1, 0, 0, 0)
        assert entry.external_attr >> 16 == 0o100755
        assert archive.read(entry) == binary.read_bytes()


def test_archive_rejects_wrong_architecture(builder, tmp_path):
    binary = tmp_path / "binary"
    binary.write_bytes(b"not an ELF")
    with pytest.raises(ValueError, match="amd64 ELF"):
        builder["package"](binary, tmp_path / "invalid.zip", "2.10.0-networklab.1")


def test_build_rejects_wrong_toolchain_before_fetch(builder, monkeypatch, tmp_path):
    def run(arguments, directory, environment):
        assert arguments == ["go", "env", "GOVERSION"]
        return "go1.26.0\n"

    monkeypatch.setitem(builder["build"].__globals__, "run", run)
    with pytest.raises(ValueError, match="Expected go1.27.3"):
        builder["build"](tmp_path / "output")


def test_build_rejects_output_symlink(builder, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic link"):
        builder["build"](link)
    assert not list(target.iterdir())


@pytest.mark.parametrize("matching", (True, False))
def test_build_publishes_only_scoped_mirror_and_receipt(builder, monkeypatch, tmp_path, matching):
    source, _ = builder["provenance"]()
    calls = []

    def run(arguments, directory, environment):
        calls.append(arguments)
        assert environment["GOMAXPROCS"] == "2"
        if arguments == ["go", "env", "GOVERSION"]:
            return "go1.27.3\n"
        if arguments == ["git", "rev-parse", "HEAD"]:
            return source["commit"] + "\n"
        if arguments == ["make", "build"]:
            (directory / "bin").mkdir()
            (directory / "bin/terraform-provider-maas").write_bytes(elf())
        return ""

    monkeypatch.setitem(builder["build"].__globals__, "run", run)
    output = tmp_path / "providers"
    fixtures = tmp_path / "source"
    fixtures.mkdir()
    patch = builder["SOURCE"] / "lifecycle.patch"
    (fixtures / "lifecycle.patch").write_bytes(patch.read_bytes())
    if matching:
        binary = tmp_path / "binary"
        binary.write_bytes(elf())
        expected_archive = tmp_path / "expected.zip"
        builder["package"](binary, expected_archive, source["version"])
        source.update(
            binary_sha256=builder["sha256"](binary),
            archive_sha256=builder["sha256"](expected_archive),
        )
    (fixtures / "source.json").write_text(json.dumps(source))
    if not matching:
        with pytest.raises(ValueError, match="pinned binary/archive SHA256"):
            builder["build"](output, fixtures)
        assert not output.exists()
        return
    receipt = builder["build"](output, fixtures)
    archive = Path(receipt["archive"])
    assert archive.parent == output / "mirror/registry.terraform.io/canonical/maas"
    assert archive.name == "terraform-provider-maas_2.10.0-networklab.1_linux_amd64.zip"
    assert receipt["archive_sha256"] == builder["sha256"](archive)
    assert archive.stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o700
    config = (output / "terraform.rc").read_text()
    assert f'path = "{output}/mirror"' in config
    assert 'include = ["registry.terraform.io/canonical/maas"]' in config
    assert 'exclude = ["registry.terraform.io/canonical/maas"]' in config
    assert "dev_overrides" not in config
    assert json.loads((output / "provider-build.json").read_text()) == receipt
    assert ["git", "fetch", "--quiet", "--depth=1", "origin", source["commit"]] in calls
    assert any(command[:3] == ["git", "apply", "--check"] for command in calls)
    assert any(command[:2] == ["make", "test"] for command in calls)


def test_provider_version_refuses_different_existing_bytes(builder, tmp_path):
    archive = tmp_path / "provider.zip"
    archive.write_bytes(b"new bytes")
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    published = mirror / archive.name
    published.write_bytes(b"existing bytes")
    with pytest.raises(ValueError, match="different archive bytes"):
        builder["stage_provider"](archive, mirror)
    assert published.read_bytes() == b"existing bytes"
    published.write_bytes(archive.read_bytes())
    builder["stage_provider"](archive, mirror)
    assert published.read_bytes() == archive.read_bytes()
