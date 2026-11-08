"""Exercise the launcher with local CLI process doubles, never infrastructure."""

import json
import os
import runpy
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def launcher(tmp_path):
    namespace = runpy.run_path(str(ROOT / "scripts/deploy_network_lab.py"))
    globals_ = namespace["main"].__globals__
    globals_.update(ROOT=tmp_path, LAB=tmp_path / "tests/network-lab")
    return namespace


def test_password_creation_reuse_and_private_mode(launcher, tmp_path):
    path = tmp_path / "private/password"
    launcher["password"](path)
    original = path.read_bytes()
    assert len(original.strip()) == 64
    path.chmod(0o644)
    launcher["password"](path)
    assert path.read_bytes() == original
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("kind", ["link", "empty", "fifo"])
def test_password_refuses_unsafe_existing_path(launcher, tmp_path, kind):
    path = tmp_path / "password"
    if kind == "link":
        target = tmp_path / "target"
        target.write_text("unchanged")
        path.symlink_to(target)
    elif kind == "empty":
        path.touch()
    else:
        os.mkfifo(path)
    with pytest.raises((ValueError, OSError)):
        launcher["password"](path)
    if kind == "link":
        assert (tmp_path / "target").read_text() == "unchanged"


@pytest.mark.parametrize(
    "cpu,access,rejected", [(2, True, True), (1, False, True), (1, True, False)]
)
def test_profile_guard_refuses_old_profile_but_allows_new_subset(
    launcher, tmp_path, cpu, access, rejected
):
    directory = tmp_path / "tests/network-lab/runtime/terraform/lab/00-networks"
    directory.mkdir(parents=True)
    state = {
        "resources": [
            {
                "mode": "managed",
                "instances": [
                    {
                        "attributes": {
                            "name": "maas",
                            "config": {"limits.cpu": str(cpu)},
                        }
                    }
                ],
            }
        ],
        "outputs": {
            "management_access_networks": {
                "value": {"a": "nlab-tf-ma", "b": "nlab-tf-mb"},
            }
        }
        if access
        else {},
    }
    (directory / "terraform.tfstate").write_text(json.dumps(state))
    if rejected:
        with pytest.raises(ValueError, match="profile|migration"):
            launcher["profile_guard"]()
    else:
        launcher["profile_guard"]()


def test_charm_source_comparison_rejects_stale_archive(launcher, tmp_path):
    common = tmp_path / "shared/src/maas_bird/__init__.py"
    common.parent.mkdir(parents=True)
    common.write_text("CURRENT = True\n")
    archives = tmp_path / "tests/network-lab/runtime/charms"
    archives.mkdir(parents=True)
    for name in ("bird", "maas-inventory", "dpu-attachments"):
        directory = tmp_path / "charms" / name
        (directory / "src").mkdir(parents=True)
        (directory / "src/charm.py").write_text("CHARM = True\n")
        (directory / "charmcraft.yaml").write_text(yaml.safe_dump({"name": name}))
        with zipfile.ZipFile(archives / f"{name}_amd64.charm", "w") as archive:
            archive.writestr("metadata.yaml", yaml.safe_dump({"name": name}))
            archive.writestr("src/charm.py", "CHARM = True\n")
            archive.writestr("venv/site-packages/maas_bird/__init__.py", common.read_bytes())
    launcher["check_charms"]()
    common.write_text("CURRENT = False\n")
    with pytest.raises(ValueError, match="shared source differs"):
        launcher["check_charms"]()


def test_duplicate_monitor_is_refused_without_signalling_it(launcher, tmp_path):
    path = tmp_path / "123/cmdline"
    path.parent.mkdir()
    path.write_bytes(b"python\0/repo/monitor.py\0--project\0maas-bird-network-lab-tf\0")
    with pytest.raises(ValueError, match="already guards"):
        launcher["reject_existing_monitor"](tmp_path)
    path.write_bytes(b"python\0/repo/monitor.py\0--project\0foreign-project\0")
    launcher["reject_existing_monitor"](tmp_path)


@pytest.fixture
def native_processes(tmp_path):
    repository = tmp_path / "repository"
    (repository / "scripts").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "scripts/deploy_network_lab.py", repository / "scripts/deploy_network_lab.py"
    )
    lab = repository / "tests/network-lab"
    (lab / "scripts").mkdir(parents=True)
    (lab / "terraform/live/lab").mkdir(parents=True)
    # Only the monitor executable is doubled. Supervision, signals, locks,
    # credentials and native-command argument/environment plumbing are real.
    (lab / "scripts/monitor.py").write_text("""
import json, os, sys, time
from pathlib import Path
output=Path(sys.argv[sys.argv.index('--output')+1])
Path(os.environ['TRACE']+'.monitor').write_text(str(os.getpid()))
with output.open('a', buffering=1) as stream:
 for index in range(1000):
  if os.environ.get('TEST_MONITOR_MODE')=='die' and index==5: sys.exit(9)
  stream.write(json.dumps({'project':'maas-bird-network-lab-tf','timestamp':time.time(),
   'temperatures_c':[] if os.environ.get('TEST_MONITOR_MODE')=='invalid' and index>=5 else [40],
   'thermal_stop':os.environ.get('TEST_MONITOR_MODE')=='trip' and index>=5})+'\\n')
  time.sleep(.05)
""")
    binary = tmp_path / "bin"
    binary.mkdir()
    tg = binary / "terragrunt"
    tg.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, signal, sys, time
from pathlib import Path
Path(os.environ['TRACE']).write_text(json.dumps({'args':sys.argv[1:], 'cwd':os.getcwd(),
 'quota':os.environ.get('TF_VAR_cpu_allowance'), 'config':os.environ.get('TF_CLI_CONFIG_FILE'),
 'binary':os.environ.get('TF_BINARY'), 'ovs':os.environ.get('TF_VAR_ovs_vsctl_path')}))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
if os.environ.get('TEST_TG_MODE')=='wait': time.sleep(60)
sys.exit(int(os.environ.get('TEST_TG_EXIT','0')))
"""
    )
    tg.chmod(0o700)
    driver = repository / "driver.py"
    driver.write_text("""
import runpy, sys
from pathlib import Path
ns=runpy.run_path(str(Path(__file__).parent/'scripts/deploy_network_lab.py'))
if __import__('os').environ.get('TEST_OVS_FILE'):
 ns['main'].__globals__['preflight']=lambda: ns['select_ovs'](
  __import__('os').environ, (__import__('os').environ['TEST_OVS_FILE'],))
elif __import__('os').environ.get('TEST_LIVE_GUARD'):
 ns['main'].__globals__['preflight']=lambda: ns['lxd_guard'](ns['profile_guard']())
else:
 ns['main'].__globals__['preflight']=lambda: None
sys.exit(ns['main']())
""")
    environment = os.environ | {
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "TRACE": str(tmp_path / "trace"),
        "TF_CLI_CONFIG_FILE": "/explicit/private/terraform.rc",
        "TF_BINARY": "/explicit/terraform",
    }
    environment.pop("TF_VAR_cpu_allowance", None)
    return repository, driver, environment


def wait_for(path, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"Missing test receipt: {path}")


def test_actual_launcher_success_reaps_guard_and_passes_native_cli(native_processes):
    repository, driver, environment = native_processes
    result = subprocess.run(
        [sys.executable, str(driver)], env=environment, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    trace = json.loads(Path(environment["TRACE"]).read_text())
    assert trace["args"] == [
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
    ]
    assert trace["quota"] == "25%"
    assert trace["config"] == environment["TF_CLI_CONFIG_FILE"]
    assert trace["binary"] == environment["TF_BINARY"]
    assert trace["cwd"] == str(repository / "tests/network-lab/terraform/live/lab")
    pid = int(Path(environment["TRACE"] + ".monitor").read_text())
    assert not Path(f"/proc/{pid}").exists()


@pytest.mark.parametrize("failure", ["native", "interrupt", "thermal"])
def test_actual_launcher_holds_guard_until_explicit_interrupt(native_processes, tmp_path, failure):
    repository, driver, environment = native_processes
    if failure == "native":
        environment["TEST_TG_EXIT"] = "17"
    else:
        environment["TEST_TG_MODE"] = "wait"
    if failure == "thermal":
        environment["TEST_MONITOR_MODE"] = "trip"
    errors = tmp_path / "stderr"
    with errors.open("w") as stream:
        process = subprocess.Popen(
            [sys.executable, str(driver)],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=stream,
        )
        try:
            wait_for(Path(environment["TRACE"]))
            if failure == "interrupt":
                process.send_signal(signal.SIGINT)
            deadline = time.monotonic() + 5
            while "STILL RUNNING" not in errors.read_text():
                assert process.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.02)
            monitor_pid = int(Path(environment["TRACE"] + ".monitor").read_text())
            assert Path(f"/proc/{monitor_pid}").exists()
            process.send_signal(signal.SIGINT)
            process.wait(timeout=5)
            assert process.returncode == {"native": 17, "interrupt": 130, "thermal": 2}[failure]
            assert not Path(f"/proc/{monitor_pid}").exists()
            receipt = next(
                (repository / "tests/network-lab/runtime/terraform").glob("deploy-*/result.json")
            )
            result = json.loads(receipt.read_text())
            if failure == "interrupt":
                assert result["interrupted"] is True
                assert result["native_exit"] == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


@pytest.mark.parametrize("monitor_mode", ["die", "invalid"])
def test_actual_launcher_cancels_apply_when_monitor_fails(native_processes, monitor_mode):
    _, driver, environment = native_processes
    result = subprocess.run(
        [sys.executable, str(driver)],
        env=environment | {"TEST_TG_MODE": "wait", "TEST_MONITOR_MODE": monitor_mode},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "Thermal monitor" in result.stderr
    assert "STILL RUNNING" not in result.stderr


def test_actual_launcher_preserves_explicit_quota(native_processes):
    _, driver, environment = native_processes
    result = subprocess.run(
        [sys.executable, str(driver)],
        env=environment | {"TF_VAR_cpu_allowance": "40%"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(Path(environment["TRACE"]).read_text())["quota"] == "40%"


def test_actual_launcher_excludes_duplicate_apply(native_processes, tmp_path):
    _, driver, environment = native_processes
    errors = tmp_path / "first.stderr"
    with errors.open("w") as stream:
        first = subprocess.Popen(
            [sys.executable, str(driver)],
            env=environment | {"TEST_TG_MODE": "wait"},
            stdout=subprocess.DEVNULL,
            stderr=stream,
        )
        try:
            wait_for(Path(environment["TRACE"]))
            second = subprocess.run(
                [sys.executable, str(driver)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert second.returncode != 0
            assert "holds the lock" in second.stderr
            first.send_signal(signal.SIGINT)
            deadline = time.monotonic() + 5
            while "STILL RUNNING" not in errors.read_text():
                assert time.monotonic() < deadline
                time.sleep(0.02)
            first.send_signal(signal.SIGINT)
            first.wait(timeout=5)
            assert first.returncode == 130
        finally:
            if first.poll() is None:
                first.kill()
                first.wait()


@pytest.mark.parametrize(
    "name",
    [
        "TF_VAR_project",
        "TF_VAR_cpu_cores",
        "TF_VAR_management_cidr",
        "TF_VAR_prefix",
        "TF_CLI_ARGS_apply",
        "TG_CONFIG",
        "TERRAGRUNT_WORKING_DIR",
        "TF_WORKSPACE",
    ],
)
def test_scope_redirect_environment_rejected_before_mutation(
    launcher, tmp_path, monkeypatch, name
):
    environment = launcher["main"].__globals__["os"].environ
    monkeypatch.setenv(name, "sensitive-wrong-target-value")
    with pytest.raises(ValueError) as error:
        launcher["environment_guard"](environment)
    assert name in str(error.value)
    assert "sensitive-wrong-target-value" not in str(error.value)
    assert not (tmp_path / "tests/network-lab/runtime").exists()


def test_environment_guard_preserves_documented_overrides(launcher):
    launcher["environment_guard"](
        {
            "TF_VAR_cpu_allowance": "25%",
            "TF_VAR_ovs_vsctl_path": "/usr/bin/ovs-vsctl",
            "TF_BINARY": "/custom/terraform",
            "TF_CLI_CONFIG_FILE": "/private/terraform.rc",
        }
    )


@pytest.mark.parametrize("name", ["TF_VAR_project", "TF_VAR_cpu_cores"])
def test_actual_launcher_rejects_wrong_scope_before_runtime_or_native_command(
    native_processes, name
):
    repository, driver, environment = native_processes
    result = subprocess.run(
        [sys.executable, str(driver)],
        env=environment | {name: "private-wrong-target"},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert name in result.stderr
    assert "private-wrong-target" not in result.stderr
    assert not Path(environment["TRACE"]).exists()
    assert not (repository / "tests/network-lab/runtime").exists()


def test_group_cleanup_handles_descendant_after_leader_exit(launcher, tmp_path):
    child = tmp_path / "child.pid"
    code = (
        "import os,time; from pathlib import Path; "
        "pid=os.fork(); "
        f"Path({str(child)!r}).write_text(str(os.getpid())) if pid==0 else None; "
        "time.sleep(60) if pid==0 else None"
    )
    process = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        wait_for(child)
        deadline = time.monotonic() + 5
        while launcher["status"](process) is None:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert process.returncode is None  # WNOWAIT has not released the leader's PID.
        assert launcher["group_running"](process.pid)
        launcher["stop"](process)
        assert process.returncode == 0
        assert not launcher["group_running"](process.pid)
    finally:
        launcher["stop"](process)


@pytest.fixture
def live_lxd_double(native_processes):
    repository, driver, environment = native_processes
    state = repository / "tests/network-lab/runtime/terraform/lab/00-networks/terraform.tfstate"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "outputs": {
                    "management_access_networks": {
                        "value": {
                            "a": "nlab-tf-ma",
                            "b": "nlab-tf-mb",
                        }
                    }
                },
                "resources": [
                    {
                        "mode": "managed",
                        "type": "lxd_instance",
                        "instances": [
                            {
                                "attributes": {
                                    "name": "maas",
                                    "project": "maas-bird-network-lab-tf",
                                    "config": {
                                        "limits.cpu": "1",
                                    },
                                },
                            }
                        ],
                    }
                ],
            }
        )
    )
    document = json.loads(state.read_text())
    document["resources"].append(
        {
            "mode": "data",
            "type": "lxd_instance",
            "name": "actual_identity",
            "instances": [
                {
                    "attributes": {
                        "name": "maas",
                        "project": "maas-bird-network-lab-tf",
                        "config": {"volatile.uuid": "owned-uuid"},
                    }
                }
            ],
        }
    )
    state.write_text(json.dumps(document))
    binary = Path(environment["PATH"].split(os.pathsep)[0]) / "lxc"
    binary.write_text(
        f"#!{sys.executable}\n"
        + """
import json,os,sys
mode=os.environ.get('TEST_LXD_MODE','valid')
args=sys.argv[1:]
if args==['remote','get-default']: print('local'); sys.exit(0)
if args==['remote','list','--format=json']:
 print(json.dumps({'local':{'Addr':
  'https://remote.invalid' if mode=='remote' else 'unix://'}})); sys.exit(0)
if mode=='api-error': print('permission denied',file=sys.stderr); sys.exit(7)
if args==['query','/1.0/projects']:
 print(json.dumps(['/1.0/projects/maas-bird-network-lab-tf'])); sys.exit(0)
if args==['query','/1.0/projects/maas-bird-network-lab-tf']:
 print(json.dumps({'config':{'user.maas-bird-owner':
  'foreign' if mode=='project' else 'network-lab'}})); sys.exit(0)
if args==['list','--project','maas-bird-network-lab-tf','--format=json']:
 print(json.dumps([{'name':'unexpected' if mode=='instance' else 'maas','config':{
 'user.maas-bird-owner':'foreign' if mode=='owner' else 'network-lab',
 'limits.cpu':'2' if mode=='cpu' else '1',
 'volatile.uuid':'changed-uuid' if mode=='uuid' else 'owned-uuid'}}])); sys.exit(0)
if args==['query','/1.0/networks?project=default&recursion=1']:
 print(json.dumps([{'name':'nlab-tf-ma','config':{'user.maas-bird-owner':
  'foreign' if mode=='carrier' else 'network-lab'}}])); sys.exit(0)
raise AssertionError(args)
"""
    )
    binary.chmod(0o700)
    return repository, driver, environment | {"TEST_LIVE_GUARD": "1"}, state


@pytest.mark.parametrize(
    "mode",
    ["project", "instance", "owner", "cpu", "uuid", "carrier", "remote", "api-error", "untracked"],
)
def test_actual_frontdoor_refuses_unsafe_live_lxd_before_credentials_or_apply(
    live_lxd_double, mode
):
    repository, driver, environment, state = live_lxd_double
    if mode == "untracked":
        state.unlink()
    result = subprocess.run(
        [sys.executable, str(driver)],
        env=environment | {"TEST_LXD_MODE": mode},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert not (repository / "tests/network-lab/runtime/terraform/maas-credentials").exists()
    assert not Path(environment["TRACE"]).exists()


def test_actual_frontdoor_allows_tracked_new_profile_subset(live_lxd_double):
    repository, driver, environment, _ = live_lxd_double
    result = subprocess.run(
        [sys.executable, str(driver), "--preflight-only"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert not (repository / "tests/network-lab/runtime/terraform/maas-credentials").exists()
    assert not Path(environment["TRACE"]).exists()


@pytest.mark.parametrize(
    "available,explicit,expected",
    [
        ((True, True), None, 0),
        ((False, True), None, 1),
        ((True, True), 1, 1),
        ((False, True), 0, None),
        ((False, False), None, None),
    ],
)
def test_prepared_ovs_selection_preserves_override_and_detects_missing(
    launcher, tmp_path, available, explicit, expected
):
    paths = tuple(str(tmp_path / name) for name in ("microovn.ovs-vsctl", "ovs-vsctl"))
    for index, exists in enumerate(available):
        if exists:
            Path(paths[index]).touch()
    environment = {} if explicit is None else {"TF_VAR_ovs_vsctl_path": paths[explicit]}
    original = environment.copy()
    if expected is None:
        with pytest.raises(ValueError, match="OVS executable"):
            launcher["select_ovs"](environment, paths)
        assert environment == original
    else:
        assert launcher["select_ovs"](environment, paths) == paths[expected]
        assert environment["TF_VAR_ovs_vsctl_path"] == paths[expected]


@pytest.mark.parametrize("identity", ["missing", "conflicting", "foreign-project"])
def test_real_provider_shape_requires_consistent_same_project_data_uuid(live_lxd_double, identity):
    _, driver, environment, state = live_lxd_double
    document = json.loads(state.read_text())
    assert "volatile.uuid" not in document["resources"][0]["instances"][0]["attributes"]["config"]
    data = document["resources"][1]
    if identity == "missing":
        document["resources"].pop()
    elif identity == "foreign-project":
        data["instances"][0]["attributes"]["project"] = "foreign"
    else:
        data["instances"].append(
            {
                "attributes": {
                    "name": "maas",
                    "project": "maas-bird-network-lab-tf",
                    "config": {"volatile.uuid": "conflicting-uuid"},
                }
            }
        )
    state.write_text(json.dumps(document))
    result = subprocess.run(
        [sys.executable, str(driver), "--preflight-only"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "UUID" in result.stderr


def test_selected_prepared_ovs_reaches_actual_native_cli_environment(native_processes, tmp_path):
    _, driver, environment = native_processes
    executable = tmp_path / "prepared-ovs"
    executable.touch()
    result = subprocess.run(
        [sys.executable, str(driver)],
        env=environment | {"TEST_OVS_FILE": str(executable)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(Path(environment["TRACE"]).read_text())["ovs"] == str(executable)
