"""Verify artifact metadata, exact sources and native extension architecture."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ELF_MACHINE = {"amd64": 62, "arm64": 183}
CHARMS = ("bird", "dpu-attachments", "maas-inventory")


def verify_payload(archive, directory):
    names = archive.namelist()
    assert "dispatch" in names
    assert not any(
        "site-packages/" in name
        and name.split("site-packages/", 1)[1].startswith(
            ("hypothesis/", "hypothesis-", "_hypothesis")
        )
        for name in names
    ), "Hypothesis must remain a development-only dependency"
    forbidden = {"policy.py", "policy_journal.py", "service_units.py"}
    assert not any(
        "site-packages/maas_bird/" in name and name.rsplit("/", 1)[-1] in forbidden
        for name in names
    ), "Retired prototype modules must not ship"
    assert not any("tests/prototype/" in name for name in names)
    assert archive.read("src/charm.py") == (directory / "src/charm.py").read_bytes()
    for source in sorted((ROOT / "shared/src/maas_bird").glob("*.py")):
        suffix = "site-packages/maas_bird/" + source.name
        matches = [name for name in names if name.endswith(suffix)]
        assert len(matches) == 1, suffix
        assert archive.read(matches[0]) == source.read_bytes(), suffix


def verify_architecture(archive, platform):
    """Check the ELF headers, not just a filename or platform declaration.

    Pydantic and YAML include native code. An amd64 wheel in a charm labelled
    arm64 would otherwise pass a source-only check and fail at the first hook.
    Runtime imports on the actual target remain a separate acceptance check.
    """
    manifest = yaml.safe_load(archive.read("manifest.yaml"))
    assert any(platform in base["architectures"] for base in manifest["bases"])
    extensions = [name for name in archive.namelist() if name.endswith(".so")]
    assert extensions, "Expected native dependency extensions in this shared runtime"
    for name in extensions:
        header = archive.read(name)[:20]
        assert header[:4] == b"\x7fELF" and header[4:6] == b"\x02\x01", name
        assert int.from_bytes(header[18:20], "little") == ELF_MACHINE[platform], name
    return len(extensions)


def verify(name, platform):
    directory = ROOT / "charms" / name
    artifact = directory / f"{name}_{platform}.charm"
    with zipfile.ZipFile(artifact) as archive:
        metadata = yaml.safe_load(archive.read("metadata.yaml"))
        declared = yaml.safe_load((directory / "charmcraft.yaml").read_text())
        assert metadata["name"] == name
        assert yaml.safe_load(archive.read("config.yaml")) == declared["config"]
        assert not metadata.get("subordinate", False)
        verify_payload(archive, directory)
        extensions = verify_architecture(archive, platform)
    return {
        "artifact": str(artifact.relative_to(ROOT)),
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "platform": platform,
        "principal": True,
        "sources_match": True,
        "hypothesis_absent": True,
        "retired_prototypes_absent": True,
        "native_extensions_verified": extensions,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("charms", nargs="*", choices=CHARMS)
    parser.add_argument("--platform", choices=tuple(ELF_MACHINE), default="amd64")
    args = parser.parse_args()
    print(json.dumps([verify(name, args.platform) for name in args.charms or CHARMS], indent=2))


if __name__ == "__main__":
    main()
