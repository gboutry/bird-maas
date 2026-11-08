"""Exercise thermal-stop ownership and project containment without stopping guests."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "network_lab_monitor", Path(__file__).parent / "scripts/monitor.py"
)
assert SPEC is not None and SPEC.loader is not None
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


def test_stop_only_running_owned_instances(monkeypatch):
    calls = []

    def command(*args):
        calls.append(args)
        if args == ("lxc", "query", "/1.0/projects"):
            return json.dumps([f"/1.0/projects/{monitor.PROJECT}"])
        return json.dumps(
            [
                {
                    "name": "maas",
                    "status": "Running",
                    "config": {"user.maas-bird-owner": "network-lab"},
                },
                {"name": "unowned-stopped", "status": "Stopped", "config": {}},
            ]
        )

    monkeypatch.setattr(monitor, "command", command)
    monitor.stop_owned()
    assert calls == [
        ("lxc", "query", "/1.0/projects"),
        ("lxc", "list", "--project", monitor.PROJECT, "--format=json"),
        ("lxc", "stop", "--project", monitor.PROJECT, "maas", "--force"),
    ]


def test_foreign_running_instance_prevents_all_stops(monkeypatch):
    calls = []

    def command(*args):
        calls.append(args)
        if args == ("lxc", "query", "/1.0/projects"):
            return json.dumps([f"/1.0/projects/{monitor.PROJECT}"])
        return json.dumps(
            [
                {
                    "name": "maas",
                    "status": "Running",
                    "config": {"user.maas-bird-owner": "network-lab"},
                },
                {"name": "foreign", "status": "Running", "config": {}},
            ]
        )

    monkeypatch.setattr(monitor, "command", command)
    with pytest.raises(RuntimeError, match="ownership"):
        monitor.stop_owned()
    assert calls == [
        ("lxc", "query", "/1.0/projects"),
        ("lxc", "list", "--project", monitor.PROJECT, "--format=json"),
    ]


def test_temperature_sources_include_cpu_package(tmp_path):
    zone = tmp_path / "class/thermal/thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "type").write_text("acpitz")
    (zone / "temp").write_text("42000")
    for number, driver, value in ((0, "k10temp", "67000"), (1, "nvme", "90000")):
        device = tmp_path / f"class/hwmon/hwmon{number}"
        device.mkdir(parents=True)
        (device / "name").write_text(driver)
        (device / "temp1_input").write_text(value)
        (device / "temp1_label").write_text("Tctl" if number == 0 else "Composite")
    assert monitor.temperature_sensors(tmp_path) == {
        "thermal/thermal_zone0/acpitz": 42,
        "cpu/hwmon0/k10temp/Tctl": 67,
    }


def test_cpu_overheat_stops_lab_even_with_cool_acpi(monkeypatch, tmp_path):
    output = tmp_path / "resources.jsonl"
    monkeypatch.setattr("sys.argv", ["monitor", "--output", str(output)])
    monkeypatch.setattr(monitor.time, "sleep", lambda _: None)
    monkeypatch.setattr(monitor, "lab_resources", lambda project: {})
    monkeypatch.setattr(
        monitor, "temperature_sensors", lambda: {"thermal/acpitz": 42, "cpu/k10temp/Tctl": 86}
    )
    stopped = []
    monkeypatch.setattr(monitor, "stop_owned", lambda project: stopped.append(True))
    with pytest.raises(SystemExit, match="thermal ceiling"):
        monitor.main()
    assert stopped == [True]
    record = json.loads(output.read_text())
    assert record["thermal_stop"] is True
    assert record["temperature_sensors_c"]["cpu/k10temp/Tctl"] == 86


def test_selected_project_contains_both_sampling_and_stops(monkeypatch, tmp_path):
    output = tmp_path / "resources.jsonl"
    monkeypatch.setattr(
        "sys.argv", ["monitor", "--project", "owned-probe", "--output", str(output)]
    )
    monkeypatch.setattr(monitor.time, "sleep", lambda _: None)
    monkeypatch.setattr(monitor, "temperature_sensors", lambda: {"cpu/k10temp/Tctl": 76})
    samples, stops = [], []

    def sample(project):
        samples.append(project)
        return {}

    monkeypatch.setattr(monitor, "lab_resources", sample)
    monkeypatch.setattr(monitor, "stop_owned", lambda project: stops.append(project))
    with pytest.raises(SystemExit, match="thermal ceiling"):
        monitor.main()
    assert samples == stops == ["owned-probe"]
    assert json.loads(output.read_text())["project"] == "owned-probe"


def test_latched_stop_catches_new_owned_instance_after_cooling(monkeypatch, tmp_path):
    output = tmp_path / "latched.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        ["monitor", "--project", "owned-probe", "--latch-stop", "--output", str(output)],
    )
    sleeps = []

    def sleep(_):
        sleeps.append(True)
        if len(sleeps) > 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(monitor.time, "sleep", sleep)
    monkeypatch.setattr(monitor, "lab_resources", lambda project: {})
    temperatures = iter((76, 42, 42))
    monkeypatch.setattr(
        monitor, "temperature_sensors", lambda: {"cpu/coretemp": next(temperatures)}
    )
    calls, stopped = [], []
    queries = []

    def command(*args):
        calls.append(args)
        if args == ("lxc", "query", "/1.0/projects"):
            return json.dumps(["/1.0/projects/owned-probe"])
        if args[1] == "list":
            queries.append(True)
            name = "maas" if len(queries) == 1 else "spine0"
            return json.dumps(
                [
                    {
                        "name": name,
                        "status": "Stopped" if name in stopped else "Running",
                        "config": {"user.maas-bird-owner": "network-lab"},
                    }
                ]
            )
        assert args[:4] == ("lxc", "stop", "--project", "owned-probe")
        stopped.append(args[4])
        return ""

    monkeypatch.setattr(monitor, "command", command)
    with pytest.raises(KeyboardInterrupt):
        monitor.main()
    assert stopped == ["maas", "spine0"]
    assert len(queries) == 3
    assert all(call[2:4] == ("--project", "owned-probe") for call in calls if call[1] != "query")
    assert sum(call == ("lxc", "query", "/1.0/projects") for call in calls) == 3
    rows = [json.loads(row) for row in output.read_text().splitlines()]
    assert len(rows) == 3
    assert [max(row["temperatures_c"]) for row in rows] == [76, 42, 42]
    assert all(row["thermal_stop"] and row["thermal_latched"] for row in rows)


def test_latch_mode_does_not_stop_a_cool_bootstrap(monkeypatch, tmp_path):
    output = tmp_path / "cool.jsonl"
    monkeypatch.setattr("sys.argv", ["monitor", "--latch-stop", "--output", str(output)])
    sleeps = []

    def sleep(_):
        sleeps.append(True)
        if len(sleeps) > 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(monitor.time, "sleep", sleep)
    monkeypatch.setattr(monitor, "lab_resources", lambda project: {})
    monkeypatch.setattr(monitor, "temperature_sensors", lambda: {"cpu/coretemp": 42})
    stopped = []
    monkeypatch.setattr(monitor, "stop_owned", lambda project: stopped.append(project))
    with pytest.raises(KeyboardInterrupt):
        monitor.main()
    assert stopped == []
    rows = [json.loads(row) for row in output.read_text().splitlines()]
    assert len(rows) == 2
    assert all(not row["thermal_stop"] and not row["thermal_latched"] for row in rows)


def test_preproject_trip_stays_latched_and_stops_only_selected_owned_guests(monkeypatch, tmp_path):
    output = tmp_path / "before-project.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        ["monitor", "--project", "owned-probe", "--latch-stop", "--output", str(output)],
    )
    samples = []

    def sleep(_):
        samples.append(True)
        if len(samples) > 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(monitor.time, "sleep", sleep)

    def sample(project):
        if len(samples) == 1:
            raise subprocess.CalledProcessError(1, ["lxc", "query"])
        return {}

    monkeypatch.setattr(monitor, "lab_resources", sample)
    temperatures = iter((76, 42, 42))
    monkeypatch.setattr(
        monitor, "temperature_sensors", lambda: {"cpu/coretemp": next(temperatures)}
    )
    inventories, calls = [], []

    def command(*args):
        calls.append(args)
        if args == ("lxc", "query", "/1.0/projects"):
            inventories.append(True)
            return json.dumps(
                ["/1.0/projects/foreign", "/1.0/projects/owned-probe-extra"]
                + (["/1.0/projects/owned-probe"] if len(inventories) > 1 else [])
            )
        if args == ("lxc", "list", "--project", "owned-probe", "--format=json"):
            return json.dumps(
                [
                    {
                        "name": "worker",
                        "status": "Running",
                        "config": {"user.maas-bird-owner": "network-lab"},
                    }
                ]
            )
        assert args == ("lxc", "stop", "--project", "owned-probe", "worker", "--force")
        return ""

    monkeypatch.setattr(monitor, "command", command)
    with pytest.raises(KeyboardInterrupt):
        monitor.main()
    assert len(inventories) == 3
    assert calls.count(("lxc", "stop", "--project", "owned-probe", "worker", "--force")) == 2
    assert not any("foreign" in arg for call in calls for arg in call)
    records = [json.loads(row) for row in output.read_text().splitlines()]
    assert records[0]["lab_sample_error"] == "CalledProcessError"
    assert [max(row["temperatures_c"]) for row in records] == [76, 42, 42]
    assert all(row["thermal_stop"] and row["thermal_latched"] for row in records)


@pytest.mark.parametrize("error", ("unauthorized", "internal server error"))
def test_project_inventory_errors_are_not_treated_as_absence(monkeypatch, error):
    calls = []
    failure = subprocess.CalledProcessError(1, ["lxc", "query"], stderr=error)

    def command(*args):
        calls.append(args)
        raise failure

    monkeypatch.setattr(monitor, "command", command)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        monitor.stop_owned("owned-probe")
    assert caught.value is failure
    assert calls == [("lxc", "query", "/1.0/projects")]


@pytest.mark.parametrize("still_exists", (False, True))
def test_failed_instance_listing_requires_confirmed_project_absence(monkeypatch, still_exists):
    calls = []
    inventory = iter((True, still_exists))
    failure = subprocess.CalledProcessError(1, ["lxc", "list"])

    def command(*args):
        calls.append(args)
        if args == ("lxc", "query", "/1.0/projects"):
            return json.dumps(["/1.0/projects/owned-probe"] if next(inventory) else [])
        raise failure

    monkeypatch.setattr(monitor, "command", command)
    if still_exists:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            monitor.stop_owned("owned-probe")
        assert caught.value is failure
    else:
        monitor.stop_owned("owned-probe")
    assert calls == [
        ("lxc", "query", "/1.0/projects"),
        ("lxc", "list", "--project", "owned-probe", "--format=json"),
        ("lxc", "query", "/1.0/projects"),
    ]


@pytest.mark.parametrize(
    "inventory", ({}, [None], [42], ["junk"], ["/1.0/instances/maas"], ["/1.0/projects/"])
)
def test_invalid_project_inventory_cannot_disable_stops(monkeypatch, inventory):
    monkeypatch.setattr(monitor, "command", lambda *args: json.dumps(inventory))
    with pytest.raises(ValueError, match="project inventory"):
        monitor.stop_owned("owned-probe")


def test_failed_instance_listing_does_not_swallow_inventory_recheck_error(monkeypatch):
    calls = []
    failure = subprocess.CalledProcessError(1, ["lxc", "query"], stderr="unauthorized")

    def command(*args):
        calls.append(args)
        if len(calls) == 1:
            return json.dumps(["/1.0/projects/owned-probe"])
        if args[1] == "list":
            raise subprocess.CalledProcessError(1, list(args))
        raise failure

    monkeypatch.setattr(monitor, "command", command)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        monitor.stop_owned("owned-probe")
    assert caught.value is failure
    assert calls == [
        ("lxc", "query", "/1.0/projects"),
        ("lxc", "list", "--project", "owned-probe", "--format=json"),
        ("lxc", "query", "/1.0/projects"),
    ]
