"""Small kernel-command model for journal tests, not a Linux networking emulator.

The real namespace suite covers actual iproute2 behavior. This model permits fast
failure injection across many journal operation sequences using real journal files.
"""

import json
import subprocess
from copy import deepcopy
from ipaddress import IPv4Network


class InjectedFailure(RuntimeError):
    pass


class KernelModel:
    def __init__(self, numeric=False):
        self.numeric = numeric
        self.rules = [
            {"src": "all", "table": table, "priority": priority}
            for table, priority in [(255, 0), (254, 32766), (253, 32767)]
        ] + [
            {
                "src": "198.18.0.0/24",
                "dst": "198.19.0.0/24",
                "priority": 900,
                "table": 7000,
                "protocol": 197,
            }
        ]
        self.routes = [
            {"dst": "198.18.0.0/24", "type": "blackhole", "table": 7000, "protocol": 197}
        ]
        self.mutations = 0
        self.fail_at = None
        self.lose_ack = False

    def snapshot(self):
        return deepcopy((self.rules, self.routes))

    def _wire(self, records, rules=False):
        records = deepcopy(records)
        if self.numeric:
            for record in records:
                for key in ("table", "protocol"):
                    if key in record:
                        record[key] = str(record[key])
                if record.get("type") == "unreachable":
                    record["type"] = "7"
                if rules:
                    for key in ("src", "dst"):
                        if record.get(key, "all") != "all":
                            network = IPv4Network(record[key])
                            record[key] = str(network.network_address)
                            if network.prefixlen != 32:
                                record[key + "len"] = network.prefixlen
        return json.dumps(records)

    def execute(self, args):
        if args == ["ip", "-j", "-N", "rule", "show"]:
            return subprocess.CompletedProcess(args, 0, self._wire(self.rules, rules=True), "")
        if args == ["ip", "-j", "-N", "route", "show", "table", "all"]:
            return subprocess.CompletedProcess(args, 0, self._wire(self.routes), "")
        assert args[:2] in (["ip", "rule"], ["ip", "route"]), args
        assert args[2] in ("add", "del"), args
        self.mutations += 1
        fail = self.mutations == self.fail_at
        if fail and not self.lose_ack:
            raise InjectedFailure("before kernel mutation")
        if args[1] == "rule":
            record = {
                "src": args[args.index("from") + 1],
                "dst": args[args.index("to") + 1],
                "priority": int(args[args.index("priority") + 1]),
                "table": int(args[args.index("table") + 1]),
                "protocol": int(args[args.index("protocol") + 1]),
            }
            records = self.rules
        else:
            assert args[3:5] == ["unreachable", "default"], args
            record = {
                "type": "unreachable",
                "dst": "default",
                "table": int(args[args.index("table") + 1]),
                "protocol": int(args[args.index("proto") + 1]),
                "metric": int(args[args.index("metric") + 1]),
            }
            records = self.routes
        if args[2] == "add":
            records.append(record)
        else:
            records.remove(record)
        if fail:
            raise InjectedFailure("after kernel mutation, before acknowledgement")
        return subprocess.CompletedProcess(args, 0, "", "")
