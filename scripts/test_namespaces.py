#!/usr/bin/env python3
"""Run root-capability tests in NEW user, mount and network namespaces only."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMESPACES = ("user", "mnt", "net")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--inside":
        parent = json.loads(os.environ["MAAS_BIRD_TEST_PARENT_NS"])
        if not all(os.readlink(f"/proc/self/ns/{ns}") != parent[ns] for ns in NAMESPACES):
            raise SystemExit("Refusing to mount or change networking outside isolated namespaces")
        subprocess.run(["mount", "--make-rprivate", "/"], check=True)
        subprocess.run(["mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", "/run"], check=True)
        Path("/run/netns").mkdir()
        os.environ["MAAS_BIRD_ISOLATED_TESTS"] = "1"
        raise SystemExit(
            subprocess.call(
                [sys.executable, "-m", "pytest", "tests/namespaces", *sys.argv[2:]], cwd=ROOT
            )
        )
    env = os.environ | {
        "MAAS_BIRD_TEST_PARENT_NS": json.dumps(
            {ns: os.readlink(f"/proc/self/ns/{ns}") for ns in NAMESPACES}
        )
    }
    raise SystemExit(
        subprocess.call(
            [
                "unshare",
                "--user",
                "--map-root-user",
                "--mount",
                "--net",
                "--fork",
                sys.executable,
                __file__,
                "--inside",
                *sys.argv[1:],
            ],
            env=env,
            cwd=ROOT,
        )
    )


if __name__ == "__main__":
    main()
