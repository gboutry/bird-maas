"""Independent command/state model; actual native boundaries are tested separately."""

import json
import re
import subprocess

from maas_bird import external_policy
from maas_bird.runtime import RuntimeErrorBase

from tests.policy_model import KernelModel


class ProvisionedHost:
    def __init__(self, plan):
        self.kernel = KernelModel()
        for context in plan.contexts:
            self.kernel.routes.append(
                dict(
                    type="unreachable",
                    dst="default",
                    table=context.table,
                    protocol=196,
                    metric=42760,
                )
            )
            for offset, source in enumerate(sorted(context.sources)):
                self.kernel.rules.append(
                    dict(
                        src=str(source),
                        dst="0.0.0.0/0",
                        table=context.table,
                        priority=context.rule_priority + offset,
                        protocol=196,
                    )
                )
        self.sources = [str(source) for c in plan.contexts for source in c.sources]
        self.calls = []
        self.active = False
        self.enabled = "disabled"
        self.generation = ""
        self.service = None
        self.confirm = "Reconfiguration confirmed"
        self.clock = 0.0
        self.fail_control = None

    def execute(self, args, check=True):
        assert self.service is not None
        self.calls.append(args)
        if args[0] == "ip":
            assert "add" not in args and "del" not in args, (
                "BIRD must never mutate provisioned policy"
            )
            if args == ["ip", "-4", "-j", "-N", "rule", "show"]:
                return subprocess.CompletedProcess(
                    args, 0, self.kernel._wire(self.kernel.rules, rules=True), ""
                )
            if args == ["ip", "-4", "-j", "-N", "route", "show", "table", "all"]:
                return subprocess.CompletedProcess(
                    args, 0, self.kernel._wire(self.kernel.routes), ""
                )
            if args == ["ip", "-4", "-j", "address", "show", "dev", "lo"]:
                addresses = [
                    {"local": p.split("/")[0], "prefixlen": int(p.split("/")[1])}
                    for p in self.sources
                ]
                return subprocess.CompletedProcess(
                    args, 0, json.dumps([{"ifname": "lo", "addr_info": addresses}]), ""
                )
            raise AssertionError(args)
        if args[0] == "bird":
            assert args[1:] == ["--version"] or args[1:3] == ["-p", "-c"]
            return subprocess.CompletedProcess(args, 0, "BIRD version 2.14\n", "")
        if args[0] == "systemctl":
            if args[1:] == ["is-failed", "--quiet", "bird.service"]:
                return subprocess.CompletedProcess(args, 1, "", "")
            if "is-enabled" in args:
                return subprocess.CompletedProcess(args, 1, self.enabled + "\n", "")
            if "is-active" in args:
                return subprocess.CompletedProcess(
                    args, 0 if self.active else 3, "active\n" if self.active else "inactive\n", ""
                )
            assert args[1] in {
                "enable",
                "disable",
                "stop",
                "start",
                "reset-failed",
                "daemon-reload",
            }, args
            if "stop" in args:
                self.active = False
            if "enable" in args and "--now" in args:
                external_policy.startup_gate(self.service.directory)
                self.generation = self.service.config.read_text()
                self.active = True
            if "disable" in args:
                self.active = False
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    def control(self, *args, check=True):
        assert self.service is not None
        if self.fail_control == args[:2]:
            raise RuntimeErrorBase("lost native acknowledgement")
        if args[0] == "configure" and args[1] not in {"undo", "confirm"}:
            self.generation = self.service.config.read_text()
            text = "Reconfigured"
        elif args[:2] == ("show", "symbols"):
            token = args[-1]
            text = token + " constant" if token in self.generation else ""
        elif args == ("show", "protocols", "all"):
            # Deliberately NO Established peers. Restrictive acceptance must be
            # independent of peer health, while still proving the loaded protocols.
            names = re.findall(r"protocol bgp (\w+)", self.generation)
            text = "\n".join(f"{name} BGP --- start 00:00:00 Active" for name in names)
        elif args == ("configure", "confirm"):
            text = self.confirm
        elif args == ("configure", "undo"):
            text = "Reconfigured"
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, text, "")
