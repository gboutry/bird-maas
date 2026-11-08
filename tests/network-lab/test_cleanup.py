"""An already removed project remains eligible for guarded cleanup finalization."""

import importlib
import json
from pathlib import Path


def test_absent_project_does_not_query_missing_project_resources(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent / "scripts"))
    cleanup = importlib.import_module("cleanup")
    monkeypatch.setattr(cleanup, "ROOT", tmp_path)
    monkeypatch.setattr(cleanup, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(cleanup, "registered_controllers", lambda: {})
    monkeypatch.setattr("sys.argv", ["cleanup.py"])

    def command(*args):
        if args[:2] == ("lxc", "query"):
            assert args[2] == "/1.0/projects?recursion=1"
            return "[]"
        assert args[0] == "terraform" and args[2:4] == ("show", "-json")
        return json.dumps({"resource_changes": []})

    monkeypatch.setattr(cleanup, "command", command)
    cleanup.main()
    receipts = list((tmp_path / "runtime").glob("cleanup-*/receipt.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["validated_instance_uuids"] == {}
    assert receipt["planned_deletions"] == []
    assert receipt["apply_requested"] is False
