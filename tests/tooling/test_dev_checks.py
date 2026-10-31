"""Real Git/installer/hook paths with a fast check-command double.

These tests prove installation ownership and commit rejection. They do not claim
that the stubbed make command executed lint, type checking or unit tests.
"""

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts/install_hooks.py"


def execute(args, repo, **options):
    # A surrounding real Git hook exports repository/index variables. Never let
    # these disposable Git subprocesses adopt or modify the operator's index.
    inherited = options.pop("env", os.environ)
    isolated = {key: value for key, value in inherited.items() if not key.startswith("GIT_")}
    isolated.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(
        args, cwd=repo, capture_output=True, text=True, check=False, env=isolated, **options
    )


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "worktree"
    repo.mkdir()
    assert execute(["git", "init", "--quiet"], repo).returncode == 0
    (repo / ".githooks").mkdir()
    shutil.copyfile(ROOT / ".githooks/pre-commit", repo / ".githooks/pre-commit")
    assert execute(["git", "add", ".githooks/pre-commit"], repo).returncode == 0
    return repo


def install(repo):
    return execute([sys.executable, str(INSTALLER)], repo)


def test_install_uses_native_hook_path_and_is_idempotent(repository):
    hook = repository / ".git/hooks/pre-commit"
    assert install(repository).returncode == 0
    before = hook.read_bytes(), hook.stat().st_ino
    assert os.access(hook, os.X_OK)
    assert before[0] == (ROOT / ".githooks/pre-commit").read_bytes()
    assert install(repository).returncode == 0
    assert (hook.read_bytes(), hook.stat().st_ino) == before


def test_install_refuses_existing_foreign_hook(repository):
    hook = repository / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\n# existing operator hook\nexit 0\n")
    before = hook.read_bytes()
    result = install(repository)
    assert result.returncode != 0
    assert "Existing pre-commit hook differs" in result.stderr
    assert hook.read_bytes() == before


def test_install_refuses_configured_hooks_directory(repository, tmp_path):
    existing = tmp_path / "shared-hooks"
    existing.mkdir()
    hook = existing / "pre-commit"
    hook.write_text("do not change")
    assert execute(["git", "config", "core.hooksPath", str(existing)], repository).returncode == 0
    result = install(repository)
    assert result.returncode != 0
    assert "core.hooksPath" in result.stderr
    assert hook.read_text() == "do not change"
    assert not (repository / ".git/hooks/pre-commit").exists()


def test_install_refuses_symbolic_link_hook(repository, tmp_path):
    victim = tmp_path / "other-hook"
    victim.write_text("do not overwrite")
    hook = repository / ".git/hooks/pre-commit"
    hook.symlink_to(victim)
    result = install(repository)
    assert result.returncode != 0
    assert "symbolic-link" in result.stderr
    assert hook.is_symlink()
    assert victim.read_text() == "do not overwrite"


def hook_environment(tmp_path, repo, status):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    make = binaries / "make"
    make.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$HOOK_LOG"\nexit "$HOOK_EXIT"\n')
    make.chmod(0o700)
    return os.environ | {
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "HOOK_LOG": str(repo / ".git/hook-command"),
        "HOOK_EXIT": str(status),
    }


@pytest.mark.parametrize("status", [0, 1])
def test_actual_git_commit_obeys_installed_check_exit(repository, tmp_path, status):
    assert install(repository).returncode == 0
    env = hook_environment(tmp_path, repository, status)
    result = execute(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "Exercise installed hook",
        ],
        repository,
        env=env,
    )
    assert (result.returncode == 0) is (status == 0)
    assert (repository / ".git/hook-command").read_text().strip() == (
        "--no-print-directory -j1 check-fast"
    )
    committed = execute(["git", "rev-parse", "--verify", "HEAD"], repository)
    assert (committed.returncode == 0) is (status == 0)


@pytest.mark.parametrize("unstaged", [True, False])
def test_actual_hook_refuses_unstaged_or_untracked_inputs(repository, tmp_path, unstaged):
    assert install(repository).returncode == 0
    if unstaged:
        with (repository / ".githooks/pre-commit").open("a") as stream:
            stream.write("# unstaged alteration\n")
    else:
        (repository / "unstaged-helper.py").write_text("# not in the prospective commit\n")
    env = hook_environment(tmp_path, repository, 0)
    result = execute([str(repository / ".git/hooks/pre-commit")], repository, env=env)
    assert result.returncode != 0
    assert "Stage or" in result.stderr
    assert not (repository / ".git/hook-command").exists()


def test_install_fails_closed_when_hook_configuration_cannot_be_read(repository, tmp_path):
    real_git = shutil.which("git")
    assert real_git is not None
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stub = binaries / "git"
    stub.write_text(
        '#!/bin/sh\nif test "$1" = config; then exit 2; fi\n'
        + "exec "
        + shlex.quote(real_git)
        + ' "$@"\n'
    )
    stub.chmod(0o700)
    env = os.environ | {"PATH": str(binaries) + os.pathsep + os.environ["PATH"]}
    result = execute([sys.executable, str(INSTALLER)], repository, env=env)
    assert result.returncode != 0
    assert "Cannot determine configured hook ownership" in result.stderr
    assert not (repository / ".git/hooks/pre-commit").exists()


@pytest.mark.slow
def test_actual_hook_budget_includes_git_prechecks(repository, tmp_path):
    assert install(repository).returncode == 0
    real_git = shutil.which("git")
    assert real_git is not None
    env = hook_environment(tmp_path, repository, 0)
    stub = tmp_path / "bin/git"
    stub.write_text(
        '#!/bin/sh\nif test "$1" = diff; then\n'
        '  printf "%s\\n" "precheck reached" > "$HOOK_PRECHECK"\n'
        "  exec sleep 20\nfi\n" + "exec " + shlex.quote(real_git) + ' "$@"\n'
    )
    stub.chmod(0o700)
    env["HOOK_PRECHECK"] = str(repository / ".git/precheck-command")
    started = time.monotonic()
    result = execute(
        [
            real_git,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "Reject incomplete prechecks",
        ],
        repository,
        env=env,
        timeout=25,
    )
    elapsed = time.monotonic() - started
    assert result.returncode != 0
    assert (repository / ".git/precheck-command").read_text().strip() == "precheck reached"
    assert not (repository / ".git/hook-command").exists()
    # A Git precheck that would take 20s must be inside the same five-second
    # budget as make. This catches accidentally timing only the later checks.
    assert 4 <= elapsed < 12
    assert execute([real_git, "rev-parse", "--verify", "HEAD"], repository).returncode != 0


@pytest.mark.parametrize("stage_change", [False, True])
def test_actual_hook_rejects_inputs_changed_during_checks(repository, tmp_path, stage_change):
    assert install(repository).returncode == 0
    env = hook_environment(tmp_path, repository, 0)
    make = tmp_path / "bin/make"
    make.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" > "$HOOK_LOG"\n'
        'printf "%s\\n" "# changed during checks" >> .githooks/pre-commit\n'
        + ("git add .githooks/pre-commit\n" if stage_change else "")
        + "exit 0\n"
    )
    result = execute(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "Reject changed check inputs",
        ],
        repository,
        env=env,
    )
    assert result.returncode != 0
    expected = "Prospective commit changed during checks" if stage_change else "Stage or stash"
    assert expected in result.stderr
    assert (repository / ".git/hook-command").exists()
    assert execute(["git", "rev-parse", "--verify", "HEAD"], repository).returncode != 0
