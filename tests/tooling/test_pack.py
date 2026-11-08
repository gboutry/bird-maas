"""Exercise the pack CLI and private artifact staging with a build-command double.

The double emits known bytes; these tests do not build or validate a real charm.
"""

import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pack_repository(tmp_path):
    repository = tmp_path / "repository"
    (repository / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/pack.py", repository / "scripts/pack.py")
    for name in ("maas-inventory", "dpu-attachments", "bird"):
        (repository / "charms" / name).mkdir(parents=True)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    builder = binaries / "charmcraft"
    builder.write_text(
        "#!/bin/sh\nset -eu\n"
        'test "$CHARMCRAFT_EXPERIMENTAL_MONOREPO" = 1\n'
        'test "$1" = pack\n'
        'name=$(basename "$PWD")\nplatform=amd64\n'
        'while test "$#" -gt 0; do\n'
        '  if test "$1" = --platform; then shift; platform=$1; fi\n'
        "  shift\ndone\n"
        'test "$name" != "${FAIL_CHARM:-}" || exit 9\n'
        'if test "$name" != "${SKIP_ARCHIVE:-}"; then\n'
        '  printf "%s" "$name:$platform:packed" > "${name}_${platform}.charm"\n'
        "fi\n"
    )
    builder.chmod(0o700)
    return repository, os.environ | {"PATH": str(binaries) + os.pathsep + os.environ["PATH"]}


def pack(fixture, *arguments, environment=None):
    repository, env = fixture
    return subprocess.run(
        [sys.executable, str(repository / "scripts/pack.py"), *arguments],
        cwd=repository,
        env=env | (environment or {}),
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_default_pack_leaves_archive_in_charm_directory(pack_repository):
    repository, _ = pack_repository
    result = pack(pack_repository, "bird")
    assert result.returncode == 0, result.stderr
    archive = repository / "charms/bird/bird_amd64.charm"
    assert archive.read_bytes() == b"bird:amd64:packed"
    assert not (repository / "tests/network-lab/runtime/charms").exists()


def test_output_stages_all_charms_when_none_are_selected(pack_repository, tmp_path):
    directory = tmp_path / "archives"
    result = pack(pack_repository, "--output-directory", str(directory))
    assert result.returncode == 0, result.stderr
    names = {"maas-inventory", "dpu-attachments", "bird"}
    assert {path.name for path in directory.iterdir()} == {f"{name}_amd64.charm" for name in names}
    for name in names:
        assert (directory / f"{name}_amd64.charm").read_bytes() == f"{name}:amd64:packed".encode()


def test_output_stages_only_requested_platform_and_charms_privately(pack_repository, tmp_path):
    repository, _ = pack_repository
    directory = tmp_path / "archives"
    directory.mkdir(mode=0o755)
    old = directory / "bird_arm64.charm"
    old.write_bytes(b"old archive")
    old.chmod(0o666)
    (repository / "charms/bird/bird_amd64.charm").write_bytes(b"other platform")
    result = pack(
        pack_repository,
        "bird",
        "dpu-attachments",
        "--platform",
        "arm64",
        "--output-directory",
        str(directory),
    )
    assert result.returncode == 0, result.stderr
    assert {path.name for path in directory.iterdir()} == {
        "bird_arm64.charm",
        "dpu-attachments_arm64.charm",
    }
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in ("bird", "dpu-attachments"):
        archive = directory / f"{name}_arm64.charm"
        assert archive.read_bytes() == f"{name}:arm64:packed".encode()
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600


def test_failed_build_does_not_stage_a_stale_archive(pack_repository, tmp_path):
    repository, _ = pack_repository
    directory = tmp_path / "archives"
    directory.mkdir()
    (repository / "charms/bird/bird_amd64.charm").write_bytes(b"stale build")
    (directory / "bird_amd64.charm").write_bytes(b"accepted artifact")
    result = pack(
        pack_repository,
        "maas-inventory",
        "bird",
        "--output-directory",
        str(directory),
        environment={"FAIL_CHARM": "bird"},
    )
    assert result.returncode != 0
    assert (directory / "maas-inventory_amd64.charm").read_bytes() == (
        b"maas-inventory:amd64:packed"
    )
    assert (directory / "bird_amd64.charm").read_bytes() == b"accepted artifact"


def test_success_without_exact_archive_fails_instead_of_staging_other_platform(
    pack_repository, tmp_path
):
    repository, _ = pack_repository
    (repository / "charms/bird/bird_amd64.charm").write_bytes(b"other platform")
    directory = tmp_path / "archives"
    result = pack(
        pack_repository,
        "bird",
        "--platform",
        "arm64",
        "--output-directory",
        str(directory),
        environment={"SKIP_ARCHIVE": "bird"},
    )
    assert result.returncode != 0
    assert "Expected built charm archive" in result.stderr
    assert not directory.exists()


def test_staging_replaces_destination_link_without_overwriting_its_target(
    pack_repository, tmp_path
):
    directory = tmp_path / "archives"
    directory.mkdir()
    foreign = tmp_path / "unrelated"
    foreign.write_bytes(b"do not change")
    archive = directory / "bird_amd64.charm"
    archive.symlink_to(foreign)
    result = pack(pack_repository, "bird", "--output-directory", str(directory))
    assert result.returncode == 0, result.stderr
    assert foreign.read_bytes() == b"do not change"
    assert not archive.is_symlink()
    assert archive.read_bytes() == b"bird:amd64:packed"
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600


def test_staging_refuses_symbolic_output_directory(pack_repository, tmp_path):
    foreign = tmp_path / "unrelated"
    foreign.mkdir(mode=0o755)
    directory = tmp_path / "archives"
    directory.symlink_to(foreign, target_is_directory=True)
    result = pack(pack_repository, "bird", "--output-directory", str(directory))
    assert result.returncode != 0
    assert "output directory must not be a symbolic link" in result.stderr
    assert list(foreign.iterdir()) == []
    assert stat.S_IMODE(foreign.stat().st_mode) == 0o755


def test_native_make_target_selects_pinned_builder_and_pipeline_artifact_directory(tmp_path):
    shutil.copyfile(ROOT / "Makefile", tmp_path / "Makefile")
    result = subprocess.run(
        ["make", "--no-print-directory", "-n", "pack-network-lab"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert shlex.split(result.stdout.replace("\\\n", " ")) == [
        "uv",
        "run",
        "--locked",
        "python",
        "scripts/pack.py",
        "--development-charmcraft",
        "--output-directory",
        "tests/network-lab/runtime/charms",
    ]
