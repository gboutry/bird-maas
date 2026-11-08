"""Build the pinned MAAS lifecycle patch into a private Terraform filesystem mirror."""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

from pack import stage_archive

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests/network-lab/terraform/provider-patches/maas"
GO_FLAGS = "-mod=readonly -p=2 -trimpath -buildvcs=false -ldflags=-buildid="


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance(directory=SOURCE):
    source = json.loads((directory / "source.json").read_text())
    patch = directory / "lifecycle.patch"
    if sha256(patch) != source["patch_sha256"]:
        raise ValueError("Provider patch does not match its pinned SHA256")
    return source, patch


def build_environment():
    return os.environ | {
        "GOMAXPROCS": "2",
        "GOTOOLCHAIN": "local",
        "GOOS": "linux",
        "GOARCH": "amd64",
        "CGO_ENABLED": "0",
        "GOFLAGS": GO_FLAGS,
    }


def run(arguments, directory, environment):
    return subprocess.check_output(arguments, cwd=directory, env=environment, text=True)


def package(binary, destination, version):
    """Use a deterministic packed mirror archive with one executable ELF payload."""
    header = binary.read_bytes()[:20]
    if header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != 62:
        raise ValueError("Expected a Linux amd64 ELF provider binary")
    entry = zipfile.ZipInfo(f"terraform-provider-maas_v{version}", (1980, 1, 1, 0, 0, 0))
    entry.create_system = 3
    entry.external_attr = 0o100755 << 16
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(entry, binary.read_bytes())
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError("Provider archive failed its CRC check")


def stage_provider(archive, mirror):
    """A prerelease version names immutable bytes, including across rebuilds."""
    existing = mirror / archive.name
    if existing.is_symlink() or (existing.exists() and sha256(existing) != sha256(archive)):
        raise ValueError("Provider version already exists with different archive bytes")
    stage_archive(archive, mirror)


def build(output, source_directory=SOURCE):
    source, patch = provenance(source_directory)
    if output.is_symlink():
        raise ValueError("The provider output directory must not be a symbolic link")
    output = output.resolve()
    environment = build_environment()
    actual_go = run(["go", "env", "GOVERSION"], ROOT, environment).strip()
    if actual_go != source["go_version"]:
        raise ValueError(f"Expected {source['go_version']}; found {actual_go}")
    with tempfile.TemporaryDirectory(prefix="maas-provider-build-") as temporary:
        checkout = Path(temporary) / "source"
        checkout.mkdir()
        run(["git", "init", "--quiet"], checkout, environment)
        run(["git", "remote", "add", "origin", source["repository"]], checkout, environment)
        run(
            ["git", "fetch", "--quiet", "--depth=1", "origin", source["commit"]],
            checkout,
            environment,
        )
        run(["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"], checkout, environment)
        if run(["git", "rev-parse", "HEAD"], checkout, environment).strip() != source["commit"]:
            raise ValueError("Provider checkout does not match the pinned commit")
        run(["git", "apply", "--check", str(patch.resolve())], checkout, environment)
        run(["git", "apply", str(patch.resolve())], checkout, environment)
        tests = run(
            [
                "make",
                "test",
                "TEST=./maas",
                "TEST_PARALLELISM=2",
                "TESTARGS=-run TestNetworkLifecycle -count=1 -p=2",
            ],
            checkout,
            environment,
        )
        run(["make", "build"], checkout, environment)
        if run(["git", "diff", "--exit-code", "--", "go.mod", "go.sum"], checkout, environment):
            raise ValueError("Provider build changed the pinned module dependencies")
        binary = checkout / "bin/terraform-provider-maas"
        archive = (
            Path(temporary)
            / f"terraform-provider-maas_{source['version']}_{source['platform']}.zip"
        )
        package(binary, archive, source["version"])
        if (
            sha256(binary) != source["binary_sha256"]
            or sha256(archive) != source["archive_sha256"]
        ):
            raise ValueError("Built provider does not match the pinned binary/archive SHA256")
        mirror = output / "mirror/registry.terraform.io/canonical/maas"
        stage_provider(archive, mirror)
        receipt = source | {
            "binary_sha256": sha256(binary),
            "archive_sha256": sha256(archive),
            "archive": str(mirror / archive.name),
            "go_flags": GO_FLAGS,
            "go_build_info": run(["go", "version", "-m", str(binary)], checkout, environment),
            "unit_test_output": tests,
        }
        configuration = (
            "provider_installation {\n"
            "  filesystem_mirror {\n"
            f"    path = {json.dumps(str(output / 'mirror'))}\n"
            '    include = ["registry.terraform.io/canonical/maas"]\n'
            "  }\n"
            '  direct { exclude = ["registry.terraform.io/canonical/maas"] }\n'
            "}\n"
        )
        for name, content in (
            ("terraform.rc", configuration),
            ("provider-build.json", json.dumps(receipt, indent=2) + "\n"),
        ):
            staged = Path(temporary) / name
            staged.write_text(content)
            stage_archive(staged, output)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    receipt = build(args.output_directory)
    print(
        json.dumps(
            {
                key: receipt[key]
                for key in ("version", "archive", "archive_sha256", "binary_sha256")
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
