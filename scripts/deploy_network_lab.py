#!/usr/bin/env python3
"""Apply the prepared native lab with the existing thermal monitor.

Host preparation, pinned image imports and isolated charm/provider builds are
prerequisites. This command does not install host software or build artifacts.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "tests/network-lab"
PROJECT = "maas-bird-network-lab-tf"
IMAGES = (
    "a80e9c9fd0d484dec024da3e9e264bbad4ce159b5312d6e149ef6ad532e18e43",
    "12a0de07e1683f41b169af0612897eae462efdeb84322281e91724c97e32fe0d",
)


def environment_guard(environment):
    unsupported = [
        name
        for name in environment
        if (
            name.startswith("TF_VAR_")
            and name
            not in {
                "TF_VAR_cpu_allowance",
                "TF_VAR_ovs_vsctl_path",
            }
        )
        or name.startswith(("TF_CLI_ARGS", "TG_", "TERRAGRUNT_"))
        or name in {"TF_DATA_DIR", "TF_WORKSPACE"}
    ]
    if unsupported:
        raise ValueError(
            "Unsupported fixed-profile environment names: " + ", ".join(sorted(unsupported))
        )


def private_directory(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"Refusing symbolic-link directory: {part}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def password(path):
    private_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
    except FileExistsError:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        created = False
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("The private MAAS password must be a regular file")
    with os.fdopen(descriptor, "w" if created else "r") as stream:
        if created:
            stream.write(secrets.token_hex(32) + "\n")
        else:
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,}\n?", stream.read()):
                raise ValueError("Invalid existing private MAAS password")
        os.fchmod(stream.fileno(), 0o600)


def check_charms():
    for name in ("bird", "maas-inventory", "dpu-attachments"):
        directory = ROOT / "charms" / name
        source = yaml.safe_load((directory / "charmcraft.yaml").read_text())
        path = LAB / "runtime/charms" / f"{name}_amd64.charm"
        if path.is_symlink():
            raise ValueError(f"Refusing symbolic-link artifact: {path}")
        with zipfile.ZipFile(path) as archive:
            if len(archive.namelist()) != len(set(archive.namelist())):
                raise ValueError(f"Duplicate charm archive members: {name}")
            if archive.testzip() is not None:
                raise ValueError(f"Corrupt charm archive: {name}")
            for section in ("config", "actions"):
                actual = (
                    yaml.safe_load(archive.read(f"{section}.yaml"))
                    if f"{section}.yaml" in archive.namelist()
                    else None
                )
                if actual != source.get(section):
                    raise ValueError(f"Rebuild {name}: {section} differs from checkout")
            metadata = yaml.safe_load(archive.read("metadata.yaml"))
            for key, value in metadata.items():
                if value != source.get("title" if key == "display-name" else key):
                    raise ValueError(f"Rebuild {name}: metadata differs from checkout")
            for file in (directory / "src").rglob("*.py"):
                if archive.read(str(file.relative_to(directory))) != file.read_bytes():
                    raise ValueError(f"Rebuild {name}: application source differs from checkout")
            for file in (ROOT / "shared/src/maas_bird").rglob("*.py"):
                suffix = "/" + str(file.relative_to(ROOT / "shared/src"))
                matches = [member for member in archive.namelist() if member.endswith(suffix)]
                if len(matches) != 1 or archive.read(matches[0]) != file.read_bytes():
                    raise ValueError(f"Rebuild {name}: shared source differs from checkout")


def profile_guard():
    states = [
        json.loads(path.read_text())
        for path in (LAB / "runtime/terraform/lab").glob("*/terraform.tfstate")
    ]
    managed = [
        instance.get("attributes", {})
        for state in states
        for resource in state.get("resources", [])
        if resource.get("mode") == "managed"
        for instance in resource.get("instances", [])
    ]
    for attributes in managed:
        if (
            attributes.get("name") in {"maas", "juju"}
            and str(attributes.get("config", {}).get("limits.cpu", "1")) != "1"
        ):
            raise ValueError(
                "Older controller CPU profile: use its reviewed migration/teardown path"
            )
    if managed:
        access = [
            state.get("outputs", {}).get("management_access_networks", {}).get("value")
            for state in states
        ]
        if not any(value == {"a": "nlab-tf-ma", "b": "nlab-tf-mb"} for value in access):
            raise ValueError(
                "Existing state lacks dedicated access carriers; implicit migration refused"
            )

    identities = {}
    for state in states:
        for resource in state.get("resources", []):
            if resource.get("mode") != "data" or resource.get("type") != "lxd_instance":
                continue
            for instance in resource.get("instances", []):
                attributes = instance.get("attributes", {})
                uuid = attributes.get("config", {}).get("volatile.uuid")
                if attributes.get("project") == PROJECT and uuid:
                    identities.setdefault(attributes["name"], set()).add(uuid)
    return {
        attributes["name"]: {
            **attributes,
            "instance_uuid": next(iter(identities[attributes["name"]]))
            if len(identities.get(attributes["name"], ())) == 1
            else None,
        }
        for state in states
        for resource in state.get("resources", [])
        if resource.get("mode") == "managed" and resource.get("type") == "lxd_instance"
        for instance in resource.get("instances", [])
        for attributes in [instance.get("attributes", {})]
        if attributes.get("project") == PROJECT
    }


def lxd_guard(tracked):
    def query(*arguments):
        return json.loads(subprocess.check_output(["lxc", *arguments], text=True, timeout=30))

    default = subprocess.check_output(
        ["lxc", "remote", "get-default"], text=True, timeout=30
    ).strip()
    remotes = query("remote", "list", "--format=json")
    if default != "local" or remotes.get("local", {}).get("Addr") != "unix://":
        raise ValueError(
            "Native deployment requires the default local remote with unix:// transport"
        )
    projects = query("query", "/1.0/projects")
    if not isinstance(projects, list) or any(
        not isinstance(url, str) or not url.startswith("/1.0/projects/") or url == "/1.0/projects/"
        for url in projects
    ):
        raise ValueError("Invalid LXD project inventory")
    if f"/1.0/projects/{PROJECT}" in projects:
        project = query("query", f"/1.0/projects/{PROJECT}")
        if project.get("config", {}).get("user.maas-bird-owner") != "network-lab":
            raise ValueError("Reserved live LXD project is not lab-owned")
        instances = query("list", "--project", PROJECT, "--format=json")
        expected = {
            "maas",
            "juju",
            "probe",
            "probe-alt",
            "probe-r2a",
            "probe-r2b",
            "spine0",
            "spine1",
            "r1a",
            "r1b",
            "r2a",
            "r2b",
            "access-a",
            "access-b",
            "gateway-r1",
            "gateway-r2",
        }
        for instance in instances:
            name, config = instance["name"], instance["config"]
            if name not in expected or config.get("user.maas-bird-owner") != "network-lab":
                raise ValueError("Reserved project contains a foreign or unexpected instance")
            if config.get("limits.cpu") != "1":
                raise ValueError("Live instance has an older or unsupported CPU profile")
            stored = tracked.get(name, {}).get("instance_uuid")
            if not stored or stored != config.get("volatile.uuid"):
                raise ValueError(
                    "Live instance is untracked or its UUID differs from private state"
                )
    networks = query("query", "/1.0/networks?project=default&recursion=1")
    for network in networks:
        if (
            network["name"] in {"nlab-tf-ma", "nlab-tf-mb"}
            and network.get("config", {}).get("user.maas-bird-owner") != "network-lab"
        ):
            raise ValueError("Reserved management-access carrier is foreign-owned")


def reject_existing_monitor(proc=Path("/proc")):
    for path in proc.glob("[0-9]*/cmdline"):
        try:
            arguments = path.read_bytes().decode().split("\0")
        except (OSError, UnicodeDecodeError):
            continue
        if any(argument.endswith("/monitor.py") for argument in arguments) and any(
            arguments[index : index + 2] == ["--project", PROJECT]
            for index in range(len(arguments) - 1)
        ):
            raise ValueError(
                "An existing monitor already guards this project; stop it before launch"
            )


def select_ovs(environment, candidates=("/snap/bin/microovn.ovs-vsctl", "/usr/bin/ovs-vsctl")):
    selected = environment.get("TF_VAR_ovs_vsctl_path")
    if selected is None:
        selected = next((path for path in candidates if Path(path).is_file()), None)
    if not selected or not Path(selected).is_file():
        raise ValueError("The selected prepared OVS executable is unavailable")
    environment["TF_VAR_ovs_vsctl_path"] = selected
    return selected


def preflight():
    tracked = profile_guard()
    reject_existing_monitor()
    for executable in ("terragrunt", "lxc", "jq", "curl", "openssl", "sudo"):
        if shutil.which(executable) is None:
            raise ValueError(f"Required prepared-host executable unavailable: {executable}")
    lxd_guard(tracked)
    subprocess.run(
        [str(LAB / "terraform/bin/terraform"), "version"], check=True, stdout=subprocess.DEVNULL
    )
    if not Path("/snap/juju/current/bin/juju").is_file():
        raise ValueError("The native profile requires /snap/juju/current/bin/juju")
    check_charms()
    providers = LAB / "runtime/terraform/providers"
    pin = json.loads((LAB / "terraform/provider-patches/maas/source.json").read_text())
    patch = LAB / "terraform/provider-patches/maas/lifecycle.patch"
    if hashlib.sha256(patch.read_bytes()).hexdigest() != pin["patch_sha256"]:
        raise ValueError("MAAS lifecycle patch differs from the pinned provider provenance")
    archive = (
        providers
        / "mirror/registry.terraform.io/canonical/maas"
        / (f"terraform-provider-maas_{pin['version']}_{pin['platform']}.zip")
    )
    if (
        archive.is_symlink()
        or hashlib.sha256(archive.read_bytes()).hexdigest() != pin["archive_sha256"]
    ):
        raise ValueError("Build the pinned lab-local MAAS provider first")
    configuration = Path(os.environ.get("TF_CLI_CONFIG_FILE", str(providers / "terraform.rc")))
    if not configuration.is_file():
        raise ValueError("The selected Terraform provider configuration is unavailable")
    for fingerprint in IMAGES:
        subprocess.run(
            ["lxc", "image", "info", fingerprint, "--project", "default"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    ovs = select_ovs(os.environ)
    authorization = ["sudo", "-n", "-l", "--", ovs, "--timeout=5"]
    for rack in (1, 2):
        for pf in (0, 1):
            authorization.extend(
                [
                    "--",
                    "set",
                    "Bridge",
                    f"nlab-tf-r{rack}-pf{pf}",
                    "other_config:forward-bpdu=true",
                ]
            )
    subprocess.run(authorization, check=True, stdout=subprocess.DEVNULL)


def status(process):
    if process.returncode is not None:
        return process.returncode
    observed = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if observed is None:
        return None
    return observed.si_status if observed.si_code == os.CLD_EXITED else -observed.si_status


def group_running(group):
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(") ", 1)[1].split()
            if int(fields[2]) == group and fields[0] != "Z":
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


def stop(process):
    if process.returncode is not None:
        return
    # An unreaped session leader reserves its PID while descendants are stopped.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    deadline = time.monotonic() + 15
    while group_running(process.pid):
        if time.monotonic() >= deadline:
            os.killpg(process.pid, signal.SIGKILL)
            break
        time.sleep(0.05)
    process.wait(timeout=5)


def sample(path):
    if not path.exists():
        return None
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - 16384))
        tail = stream.read()
    lines = tail.split(b"\n")
    if len(lines) < 2:
        return None
    record = json.loads(lines[-2])
    if not isinstance(record, dict):
        raise RuntimeError("Thermal monitor sample is not an object")
    temperatures = record.get("temperatures_c")
    timestamp = record.get("timestamp")
    if (
        record.get("project") != PROJECT
        or not isinstance(temperatures, list)
        or not temperatures
        or any(
            type(value) not in (int, float) or not math.isfinite(value) for value in temperatures
        )
        or not isinstance(record.get("thermal_stop"), bool)
        or type(timestamp) not in (int, float)
        or not math.isfinite(timestamp)
        or not 0 <= time.time() - timestamp < 15
    ):
        raise RuntimeError("Thermal monitor sample is invalid, missing temperatures or stale")
    return record


def apply(directory):
    log = directory / "resources.jsonl"
    with (directory / "monitor.stderr").open("w") as errors:
        monitor = subprocess.Popen(
            [
                sys.executable,
                str(LAB / "scripts/monitor.py"),
                "--project",
                PROJECT,
                "--ceiling-c",
                "75",
                "--interval",
                "2",
                "--latch-stop",
                "--output",
                str(log),
            ],
            stderr=errors,
            start_new_session=True,
        )
        deployment = None
        code = 1
        guarded = False
        interrupted = False
        try:
            try:
                deadline = time.monotonic() + 45
                while sample(log) is None:
                    if status(monitor) is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Thermal monitor failed before deployment")
                    time.sleep(0.2)
                guarded = True
                if sample(log)["thermal_stop"]:
                    code = 2
                    print("Thermal latch is active; deployment not started", file=sys.stderr)
                else:
                    environment = os.environ | {
                        "TF_VAR_cpu_allowance": os.environ.get("TF_VAR_cpu_allowance", "25%")
                    }
                    deployment = subprocess.Popen(
                        [
                            "terragrunt",
                            "run",
                            "--all",
                            "--parallelism",
                            "2",
                            "--non-interactive",
                            "--",
                            "apply",
                            "-input=false",
                            "-auto-approve",
                            "-parallelism=4",
                        ],
                        cwd=LAB / "terraform/live/lab",
                        env=environment,
                        start_new_session=True,
                    )
                    while status(deployment) is None:
                        if status(monitor) is not None:
                            guarded = False
                            raise RuntimeError("Thermal monitor exited during deployment")
                        try:
                            record = sample(log)
                            if record is None:
                                raise RuntimeError("Thermal monitor sample unavailable")
                        except (ValueError, RuntimeError, KeyError):
                            guarded = False
                            raise
                        if record["thermal_stop"]:
                            code = 2
                            print("Thermal latch interrupted deployment", file=sys.stderr)
                            break
                        time.sleep(0.2)
                    else:
                        if status(monitor) is not None:
                            guarded = False
                            raise RuntimeError(
                                "Thermal monitor exited before deployment completed"
                            )
                        final = sample(log)
                        if final is None:
                            guarded = False
                            raise RuntimeError("Thermal monitor final sample unavailable")
                        code = 2 if final["thermal_stop"] else status(deployment)
            except KeyboardInterrupt:
                interrupted = True
                code = 130
                print(
                    "Deployment interrupted; treating any native exit 0 as failure",
                    file=sys.stderr,
                )
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                code = 1
                guarded = False
                print(str(error), file=sys.stderr)
            finally:
                if deployment is not None:
                    stop(deployment)
            if code != 0 and guarded and status(monitor) is None:
                print(
                    f"Deployment exit {code}; thermal guard STILL RUNNING in foreground. "
                    "Guest installation may continue. "
                    "Press Ctrl-C to stop/reap this guard and exit.",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    while status(monitor) is None:
                        record = sample(log)
                        if record is None:
                            raise RuntimeError(
                                "Thermal monitor sample unavailable during failure hold"
                            )
                        time.sleep(0.2)
                    print("Thermal guard exited during failure hold", file=sys.stderr)
                except KeyboardInterrupt:
                    pass
                except (OSError, ValueError, RuntimeError, KeyError) as error:
                    print(f"Thermal guard unusable during failure hold: {error}", file=sys.stderr)
            (directory / "result.json").write_text(
                json.dumps(
                    {
                        "project": PROJECT,
                        "deployment_exit": code,
                        "interrupted": interrupted,
                        "native_exit": deployment.returncode if deployment is not None else None,
                    }
                )
                + "\n"
            )
            return code if code >= 0 else 128 - code
        finally:
            if deployment is not None:
                stop(deployment)
            stop(monitor)


def interrupt(_signal, _frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupt)
    environment_guard(os.environ)
    os.umask(0o077)
    runtime = LAB / "runtime/terraform"
    private_directory(runtime)
    descriptor = os.open(runtime / "deploy.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another native lab deployment holds the lock") from error
        preflight()
        if args.preflight_only:
            return 0
        password(runtime / "maas-credentials/admin-password")
        directory = Path(tempfile.mkdtemp(prefix="deploy-", dir=runtime))
        print(
            f"APPLY {PROJECT}: 16 allocated CPUs/38 GiB; ceiling75C; "
            f"Terragrunt2/Terraform4. Private evidence: {directory}",
            flush=True,
        )
        return apply(directory)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
