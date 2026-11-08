"""Install the pinned upstream Containerlab package inside the owned fabric VM."""

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen


def main():
    lock = json.loads(Path("/etc/network-lab/versions.json").read_text())["containerlab"]
    url = urlparse(lock["url"])
    expected_name = f"containerlab_{lock['version']}_linux_{lock['architecture']}.deb"
    if (
        url.scheme != "https"
        or url.netloc != "github.com"
        or url.path
        != f"/srl-labs/containerlab/releases/download/v{lock['version']}/{expected_name}"
    ):
        raise RuntimeError("Unexpected upstream package URL")
    with tempfile.TemporaryDirectory(prefix="network-lab-", dir="/var/tmp") as temporary:
        directory = Path(temporary)
        directory.chmod(0o755)
        artifact = directory / expected_name
        with urlopen(lock["url"], timeout=300) as response:
            artifact.write_bytes(response.read())
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != lock["sha256"]:
            raise RuntimeError("Containerlab package checksum mismatch")
        artifact.chmod(0o644)
        for field, expected in (
            ("Package", "containerlab"),
            ("Architecture", lock["architecture"]),
            ("Version", lock["version"]),
        ):
            observed = subprocess.check_output(
                ["dpkg-deb", "--field", str(artifact), field], text=True, timeout=30
            ).strip()
            if observed != expected:
                raise RuntimeError(f"Unexpected package {field}")
        subprocess.run(
            ["apt-get", "install", "--yes", str(artifact)],
            check=True,
            timeout=600,
            env=os.environ | {"DEBIAN_FRONTEND": "noninteractive"},
        )
    receipt = {
        "containerlab": lock,
        "containerlab_cli": subprocess.check_output(
            ["containerlab", "version"], text=True, timeout=30
        ),
        "docker_package": subprocess.check_output(
            ["dpkg-query", "-W", "docker.io"], text=True, timeout=30
        ),
    }
    output = Path("/var/lib/network-lab")
    output.mkdir(parents=True, exist_ok=True)
    (output / "bootstrap-versions.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
