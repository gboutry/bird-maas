"""Check principal metadata and that packed shared/charm sources match this checkout."""

import hashlib
import json
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
results = []
for directory in sorted((ROOT / "charms").iterdir()):
    if not directory.is_dir():
        continue
    artifact = directory / f"{directory.name}_amd64.charm"
    with zipfile.ZipFile(artifact) as archive:
        metadata = yaml.safe_load(archive.read("metadata.yaml"))
        declared = yaml.safe_load((directory / "charmcraft.yaml").read_text())
        assert metadata["name"] == directory.name
        assert yaml.safe_load(archive.read("config.yaml")) == declared["config"]
        assert "dispatch" in archive.namelist()
        assert not metadata.get("subordinate", False)
        assert not any(
            "site-packages/" in name
            and name.split("site-packages/", 1)[1].startswith(
                ("hypothesis/", "hypothesis-", "_hypothesis")
            )
            for name in archive.namelist()
        ), "Hypothesis must remain a development-only dependency"
        assert archive.read("src/charm.py") == (directory / "src/charm.py").read_bytes()
        for source in sorted((ROOT / "shared/src/maas_bird").glob("*.py")):
            suffix = "site-packages/maas_bird/" + source.name
            matches = [name for name in archive.namelist() if name.endswith(suffix)]
            assert len(matches) == 1, suffix
            assert archive.read(matches[0]) == source.read_bytes(), suffix
    results.append(
        {
            "artifact": str(artifact.relative_to(ROOT)),
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "principal": True,
            "sources_match": True,
            "hypothesis_absent": True,
        }
    )
print(json.dumps(results, indent=2))
