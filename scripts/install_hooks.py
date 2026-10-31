#!/usr/bin/env python3
"""Opt in to this repository's hook without replacing existing hook ownership."""

import os
import subprocess
from pathlib import Path


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def install():
    root_result = git("rev-parse", "--show-toplevel")
    if root_result.returncode:
        raise ValueError("Run from a Git working tree")
    root = Path(root_result.stdout.strip())
    configured = git("config", "--get", "core.hooksPath")
    if configured.returncode == 0:
        raise ValueError("Existing core.hooksPath is configured; no hook was changed")
    if configured.returncode != 1:
        raise ValueError("Cannot determine configured hook ownership; no hook was changed")
    result = git("rev-parse", "--git-path", "hooks/pre-commit")
    if result.returncode:
        raise ValueError("Cannot resolve this repository's native hook path")
    target = Path(result.stdout.strip())
    if not target.is_absolute():
        target = Path.cwd() / target
    source = root / ".githooks/pre-commit"
    contents = source.read_bytes()
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("Refusing a symbolic-link hook path")
    if target.exists():
        if target.read_bytes() == contents and os.access(target, os.X_OK):
            return target
        raise ValueError("Existing pre-commit hook differs; no hook was changed")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())
    return target


if __name__ == "__main__":
    try:
        print(f"Installed repository pre-commit hook: {install()}")
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
