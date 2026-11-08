"""Execute the real wrapper with isolated paths and a reporting Terraform stub."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_artifacts import ROOT


@pytest.mark.parametrize(
    "mirror_exists,override",
    ((False, None), (True, None), (True, "/explicit/custom.rc"), (True, "")),
)
def test_wrapper_selects_only_an_existing_unset_lab_cli_config(tmp_path, mirror_exists, override):
    lab = tmp_path / "isolated lab"
    wrapper = lab / "terraform/bin/terraform"
    wrapper.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "terraform/bin/terraform", wrapper)
    config = lab / "runtime/terraform/providers/terraform.rc"
    if mirror_exists:
        config.parent.mkdir(parents=True)
        config.write_text("# Isolated mirror configuration\n")
    stub = tmp_path / "reporting-terraform"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "mask = os.umask(0)\n"
        "os.umask(mask)\n"
        "print(json.dumps({'config': os.environ.get('TF_CLI_CONFIG_FILE'), "
        "'umask': mask, 'args': sys.argv[1:]}))\n"
    )
    stub.chmod(0o700)
    env = {**os.environ, "TF_BINARY": str(stub)}
    env.pop("TF_CLI_CONFIG_FILE", None)
    if override is not None:
        env["TF_CLI_CONFIG_FILE"] = override
    result = subprocess.run(
        [str(wrapper), "version", "argument with spaces"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    observed = json.loads(result.stdout)
    expected = override if override is not None else str(config) if mirror_exists else None
    assert observed == {
        "config": expected,
        "umask": 0o077,
        "args": ["version", "argument with spaces"],
    }
    if mirror_exists and override is None:
        assert Path(observed["config"]).is_absolute()
    assert config.exists() == mirror_exists


@pytest.mark.parametrize("binary", ("relative-terraform", "/nonexistent/lab-terraform"))
def test_wrapper_retains_binary_validation(tmp_path, binary):
    result = subprocess.run(
        [str(ROOT / "terraform/bin/terraform"), "version"],
        cwd=tmp_path,
        env={**os.environ, "TF_BINARY": binary},
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Terraform" in result.stderr or "TF_BINARY" in result.stderr
