"""Build a deterministic stdlib-only network toolkit tarball from explicit inputs."""

import argparse
import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "LICENSE",
    "scripts/network_benchmark.py",
    "scripts/network_benchmark_agent.py",
    "scripts/network_benchmark_ceph.py",
    "scripts/network_benchmark_lease.py",
    "docs/network-benchmark.md",
    "examples/network-benchmark/inventory.json",
    "examples/network-benchmark/manifest.json",
)
RENDERER_FILES = ("README.md", "bin/report.mjs", "src/report.mjs", "src/chart.mjs", "src/hover.js")
LAUNCHER = b'''#!/usr/bin/env python3
"""Run the adjacent bundled controller without installing Python packages."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parent / "scripts/network_benchmark.py"),
               run_name="__main__")
'''


def source(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular source file: {path}")
    return path.read_bytes()


def package(destination, renderer=None, root=ROOT):
    files = {name: source(root / name) for name in FILES}
    files["README.md"] = source(root / "examples/network-benchmark/QUICKSTART.md")
    files["network_benchmark.py"] = LAUNCHER
    if renderer is not None:
        files.update({f"renderer/{name}": source(renderer / name) for name in RENDERER_FILES})
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}
    files["BUNDLE.json"] = (
        json.dumps(
            {
                "version": 1,
                "boundary": "Python standard-library scripts; Python/SSH and optional OS tools "
                "must already be installed. No OS runtimes or binaries are bundled.",
                "renderer_included": renderer is not None,
                "sha256": hashes,
            },
            indent=2,
        )
        + "\n"
    ).encode()
    files["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(files.items())
    ).encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Build before publishing; the small allowlisted sources contain no evidence.
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, data in sorted(files.items()):
                entry = tarfile.TarInfo("network-benchmark/" + name)
                entry.size = len(data)
                entry.mode = 0o755 if name == "network_benchmark.py" else 0o644
                archive.addfile(entry, io.BytesIO(data))
    with destination.open("xb") as stream:
        stream.write(output.getvalue())
    return hashlib.sha256(output.getvalue()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--renderer-source", type=Path)
    args = parser.parse_args()
    digest = package(args.output, args.renderer_source)
    print(f"{digest}  {args.output}")


if __name__ == "__main__":
    main()
