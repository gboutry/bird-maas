"""Small kernel-command model for journal tests, not a Linux networking emulator.

The real namespace suite covers actual iproute2 behavior. This model permits fast
failure injection across many journal operation sequences using real journal files.
"""

import json
import subprocess
from copy import deepcopy
from ipaddress import IPv4Network


class InjectedFailure(RuntimeError):
    """Fail a modeled command before its effect or after its lost acknowledgement."""


class KernelModel:
    """Model only the snapshot/add/delete commands used by the ownership journal.

    Stored records use a canonical representation; ``numeric`` varies only the
    serialized iproute2 response. This deliberately small model does not cover
    route selection, reboot, or every route selector: use namespace tests there.
    """

    def __init__(self, numeric=False):
        self.numeric = numeric
        # Kernel defaults plus unrelated operator-owned objects form the oracle's
        # baseline. Cleanup must preserve those foreign objects byte-for-byte.
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
        """Return an independent oracle snapshot that later mutations cannot alter."""
        return deepcopy((self.rules, self.routes))

    def _wire(self, records, rules=False):
        records = deepcopy(records)
        if self.numeric:
            for record in records:
                self._numeric_record(record, rules)
        return json.dumps(records)

    @staticmethod
    def _numeric_record(record, rules):
        """Reproduce the numeric fields and split prefix lengths seen in ip -N JSON."""
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

    def execute(self, args):
        """Serve snapshots or perform one mutation with a precisely located fault."""
        # Reads never consume a failure position: fail_at counts mutations only.
        if args == ["ip", "-j", "-N", "rule", "show"]:
            return subprocess.CompletedProcess(args, 0, self._wire(self.rules, rules=True), "")
        if args == ["ip", "-4", "-j", "-N", "route", "show", "table", "all"]:
            return subprocess.CompletedProcess(args, 0, self._wire(self.routes), "")
        # New production commands must extend the model explicitly; a fake success
        # for an unsupported command would conceal a verification gap.
        assert args[:2] in (["ip", "rule"], ["ip", "route"]), args
        assert args[2] in ("add", "del"), args
        self.mutations += 1
        fail = self.mutations == self.fail_at
        # Known failure: the kernel did not apply this operation.
        if fail and not self.lose_ack:
            raise InjectedFailure("before kernel mutation")
        records, record = self._mutation_record(args)
        if args[2] == "add":
            records.append(record)
        else:
            records.remove(record)
        # Uncertain failure: the effect exists, but its acknowledgement was lost.
        # The production journal must refuse to adopt or delete this operation.
        if fail:
            raise InjectedFailure("after kernel mutation, before acknowledgement")
        return subprocess.CompletedProcess(args, 0, "", "")

    def _mutation_record(self, args):
        """Decode supported commands independently of the production parser."""
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
        return records, record
