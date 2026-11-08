"""Verify native Juju secret contents without exposing values in assertions."""

import json
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_native_secret_contents_and_private_state():
    path = ROOT / "runtime/terraform/lab/60-secrets/terraform.tfstate"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    state = json.loads(path.read_text())
    outputs = {key: value["value"] for key, value in state["outputs"].items()}
    environment = {**os.environ, "JUJU_DATA": str(ROOT / "runtime/terraform/juju-client")}

    def read(uri):
        result = subprocess.run(
            [
                "juju",
                "show-secret",
                uri,
                "--model=nlab-tf:network-lab",
                "--reveal",
                "--format=json",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, "Native secret retrieval failed"
        rows = json.loads(result.stdout)
        assert len(rows) == 1
        return next(iter(rows.values()))["content"]["Data"]

    assert set(read(outputs["maas_secret_uri"])) == {"api-key"}
    assert set(read(outputs["maas_ca_secret_uri"])) == {"ca-cert"}
    assert len(outputs["bgp_secret_uris"]) == 4
    seen = set()
    for name, uri in outputs["bgp_secret_uris"].items():
        data = read(uri)
        expected = {key for key in outputs["bgp_passwords"] if key.startswith(name + "-")}
        # "probe" is also the prefix of the other host names.
        if name == "probe":
            expected -= {
                key
                for key in expected
                if key.startswith(("probe-alt-", "probe-r2a-", "probe-r2b-"))
            }
        assert len(data) == 10
        assert set(data) == expected
        for key, value in data.items():
            matches = value == outputs["bgp_passwords"][key]
            valid = len(value) == 32 and value.isalnum()
            unique = value not in seen
            assert matches, "Secret/state mismatch"
            assert valid, "Invalid credential format"
            assert unique, "Duplicate session credential"
            seen.add(value)
    assert len(seen) == 40
