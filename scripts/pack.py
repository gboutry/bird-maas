#!/usr/bin/env python3
"""Pack principal charms using Charmcraft's experimental monorepo support."""

import argparse
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHARMCRAFT_REVISION = "512458868b2a7e33ce6eb39a9b7a7b0a1f40f347"
CHARMS = ("maas-inventory", "dpu-attachments", "bird")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("charms", nargs="*", choices=CHARMS)
    parser.add_argument("--platform", choices=("amd64", "arm64"), default="amd64")
    parser.add_argument(
        "--development-charmcraft",
        action="store_true",
        help="Use a pinned project-local Charmcraft build; do not refresh the snap",
    )
    parser.add_argument(
        "--clean", action="store_true", help="Clean native build parts before packing"
    )
    args = parser.parse_args()
    executable = ["charmcraft"]
    if args.development_charmcraft:
        executable = [
            "uv",
            "tool",
            "run",
            "--python",
            "3.12",
            "--from",
            f"git+https://github.com/canonical/charmcraft.git@{CHARMCRAFT_REVISION}",
            "charmcraft",
        ]
    env = os.environ | {"CHARMCRAFT_EXPERIMENTAL_MONOREPO": "1"}
    if args.development_charmcraft:
        # A Python-installed outer tool otherwise installs latest/stable (4.4) inside LXD.
        # The monorepo-aware uv plugin is required in BOTH environments.
        env["CRAFT_SNAP_CHANNEL"] = os.environ.get("CRAFT_SNAP_CHANNEL", "latest/candidate")
    for name in args.charms or CHARMS:
        if args.clean:
            subprocess.run(
                [*executable, "clean", "charm", "--use-lxd", "--platform", args.platform],
                cwd=ROOT / "charms" / name,
                env=env,
                check=True,
            )
        subprocess.run(
            [*executable, "pack", "--use-lxd", "--platform", args.platform],
            cwd=ROOT / "charms" / name,
            env=env,
            check=True,
        )


if __name__ == "__main__":
    main()
