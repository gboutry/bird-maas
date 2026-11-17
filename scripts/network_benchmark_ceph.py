"""Read-only Ceph guard collection and fail-closed policy; never generates test traffic.

The collector runs on the SSH bastion. The policy runs in the controller's clock
domain. Interval means are delta(sum seconds)/delta(completed operations), not
percentiles of requests. Preserve the raw command records alongside decisions.
"""

import argparse
import base64
import concurrent.futures
import copy
import json
import math
import re
import shlex
import subprocess
import time
from pathlib import Path

LATENCIES = ("op_latency", "op_r_latency", "op_w_latency", "op_rw_latency", "subop_latency")
SSH = [
    "ssh",
    "-T",
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "ConnectTimeout=3",
    "-o",
    "ConnectionAttempts=1",
]

# This source executes only local read commands on an allowed OSD host. Each
# command is recorded before execution in the returned transcript. Check the
# service identity on both sides of the socket read to reject a concurrent restart.
LOCAL = """import concurrent.futures,json,subprocess,time
from pathlib import Path
config=CONFIG
if Path('/etc/machine-id').read_text().strip()!=config['machine_id']:
    raise ValueError('machine identity mismatch')
def run(argv):
    r={'argv':argv,'started':time.time()}
    try:
        p=subprocess.run(argv,capture_output=True,text=True,timeout=3)
        r.update(returncode=p.returncode,stdout=p.stdout,stderr=p.stderr)
    except Exception as e:r.update(returncode=None,error=str(e))
    r['ended']=time.time()
    return r
def one(osd):
    def state():
        r=run(['systemctl','show',f'ceph-osd@{osd}.service',
               '--property=Id,MainPID,ActiveState,SubState'])
        if r['returncode']!=0:raise RuntimeError(r)
        s=dict(x.split('=',1) for x in r['stdout'].splitlines() if '=' in x)
        assert s['ActiveState']=='active' and s['SubState']=='running',s
        pid=int(s['MainPID']);assert pid>1,s
        return [pid,Path(f'/proc/{pid}/stat').read_text().rsplit(') ',1)[1].split()[19]],r
    before,a=state()
    p=run(['ceph','daemon',f'osd.{osd}','perf','dump'])
    after,b=state()
    assert before==after,'OSD restarted during read'
    assert p['returncode']==0,p
    return str(osd),{'process':before,'perf':json.loads(p['stdout'])['osd'],
                     'commands':[a,p,b]}
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    print(json.dumps(dict(pool.map(one,config['osds']))),flush=True)
"""


def remote_python(source):
    """Keep the command on one line across sudo's login-shell quoting."""
    encoded = base64.b64encode(source.encode()).decode()
    return shlex.join(
        ["sudo", "-n", "python3", "-c", f"import base64;exec(base64.b64decode({encoded!r}))"]
    )


def collect(config, record):
    """Concurrent bounded reads; record intent before starting every SSH command."""
    targets = [config["monitor"], *(h["ssh"] for h in config["hosts"])]
    pattern = r"(?:[a-zA-Z0-9_.-]+@)?[a-zA-Z0-9][a-zA-Z0-9_.-]*"
    if any(not isinstance(t, str) or not re.fullmatch(pattern, t) for t in targets):
        raise ValueError("Invalid SSH target")
    identities, osd_ids = set(), set()
    for host in config["hosts"]:
        identity = host["machine_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", identity) or identity in identities:
            raise ValueError("Invalid/duplicate machine identity")
        identities.add(identity)
        for osd in host["osds"]:
            if type(osd) is not int or osd < 0 or osd in osd_ids:
                raise ValueError("Invalid/duplicate OSD ID")
            osd_ids.add(osd)
    started = time.monotonic()
    requests = {
        name: [*SSH, config["monitor"], shlex.join(["sudo", "-n", "ceph", *args])]
        for name, args in (
            ("status", ["status", "--format=json"]),
            ("health", ["health", "detail", "--format=json"]),
            ("tree", ["osd", "tree", "--format=json"]),
        )
    }
    for host in config["hosts"]:
        source = LOCAL.replace("CONFIG", repr(host))
        requests[host["ssh"]] = [*SSH, host["ssh"], remote_python(source)]
    for name, argv in requests.items():
        record({"event": "command", "name": name, "argv": argv, "time": time.time()})

    def run(item):
        name, argv = item
        result = {"event": "result", "name": name, "argv": argv, "started": time.time()}
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=8)
            result.update(returncode=p.returncode, stdout=p.stdout, stderr=p.stderr)
            if p.returncode:
                raise ValueError(f"{name}: read failed ({p.returncode}): {p.stderr}")
            return name, json.loads(p.stdout), result
        except Exception as error:
            result["error"] = str(error)
            return name, None, result
        finally:
            result["ended"] = time.time()

    values, errors = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(requests)) as pool:
        for name, value, result in pool.map(run, requests.items()):
            record(result)
            if value is None:
                errors.append(result["error"])
            else:
                values[name] = value
    ended = time.monotonic()
    if errors:
        raise ValueError("; ".join(errors))
    osds = {}
    for host in config["hosts"]:
        for osd, value in values.pop(host["ssh"]).items():
            if osd in osds:
                raise ValueError("Duplicate OSD identity")
            osds[osd] = value
    return dict(started=started, ended=ended, osds=osds, **values)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Missing/non-numeric counter")
    if not math.isfinite(value) or value < 0:
        raise ValueError("Invalid counter")
    return value


def signature(snapshot):
    """Freeze health details and membership, not volatile I/O or election ages."""
    status = snapshot["status"]
    health = snapshot["health"]
    if status["health"]["status"] != health["status"]:
        raise ValueError("Health changed during collection")
    # Status lacks detail messages; require its summaries to match the detail read.
    summaries = copy.deepcopy(health["checks"])
    for value in summaries.values():
        value.pop("detail", None)
    if status["health"]["checks"] != summaries:
        raise ValueError("Health summaries changed during collection")
    pg = status["pgmap"]
    if pg["pgs_by_state"] != [{"state_name": "active+clean", "count": pg["num_pgs"]}]:
        raise ValueError("PGs are not all active+clean")
    if status["osdmap"]["num_remapped_pgs"] != 0:
        raise ValueError("Remapped PGs")
    quorum = sorted(status["quorum_names"])
    if len(quorum) != status["monmap"]["num_mons"] or len(set(quorum)) != len(quorum):
        raise ValueError("Incomplete monitor quorum")
    membership = {
        str(node["id"]): [node["status"], node["reweight"]]
        for node in snapshot["tree"]["nodes"]
        if node["type"] == "osd"
    }
    if not membership:
        raise ValueError("Empty OSD tree")
    for osd in snapshot["osds"]:
        if membership[osd][0] != "up" or membership[osd][1] <= 0:
            raise ValueError(f"Target OSD {osd} not up/in")
    return {
        "fsid": status["fsid"],
        "health": health,
        "quorum": quorum,
        "membership": membership,
        "pgs": pg["num_pgs"],
        "pools": pg["num_pools"],
        "osd_counts": {
            key: status["osdmap"][key] for key in ("num_osds", "num_up_osds", "num_in_osds")
        },
        "processes": {key: value["process"] for key, value in snapshot["osds"].items()},
    }


class Guard:
    """A trip is latched. Missing/idle measurements never become zero latency."""

    def __init__(
        self, expected, osds, minimum_seconds=300, minimum_intervals=10, fixed_floor_osds=()
    ):
        self.expected = expected
        self.osds = set(map(str, osds))
        self.fixed_floor_osds = set(map(str, fixed_floor_osds))
        if not self.fixed_floor_osds <= self.osds:
            raise ValueError("Fixed-floor exceptions must be explicit target OSDs")
        self.minimum_seconds = minimum_seconds
        self.minimum_intervals = minimum_intervals
        self.first = None
        self.previous = None
        self.reference = None
        self.history = {f"{osd}/{metric}": [] for osd in self.osds for metric in LATENCIES}
        self.thresholds = None
        self.strikes = {}
        self.stalled = {}
        self.tripped = None

    def trip(self, reason):
        self.tripped = str(reason)
        raise ValueError(self.tripped)

    def check(self, snapshot, now=None):
        try:
            return self._check(snapshot, time.monotonic() if now is None else now)
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            self.trip(error)

    def _check(self, snapshot, now):
        if self.tripped is not None:
            raise ValueError(self.tripped)
        start, end = snapshot["started"], snapshot["ended"]
        if not (0 <= now - start <= 10 and start <= end <= now):
            raise ValueError("Stale/invalid Ceph snapshot (>10 seconds)")
        if self.previous and not (0 < start - self.previous["started"] <= 10):
            raise ValueError("Ceph collection gap/reordered snapshot")
        if set(snapshot["osds"]) != self.osds:
            raise ValueError("Missing/unexpected target OSDs")
        sig = signature(snapshot)
        if sig["health"] != self.expected:
            raise ValueError("Unexpected Ceph health detail")
        if self.reference is not None and sig != self.reference:
            raise ValueError("Ceph quorum/membership/process/PG drift")
        means = {}
        for osd, current in snapshot["osds"].items():
            perf = current["perf"]
            wip = number(perf["op_wip"])
            for metric in LATENCIES:
                count = number(perf[metric]["avgcount"])
                total = number(perf[metric]["sum"])
                if not isinstance(count, int):
                    raise ValueError("Non-integer operation count")
                if not self.previous:
                    continue
                old = self.previous["osds"][osd]["perf"][metric]
                n, seconds = count - old["avgcount"], total - old["sum"]
                if n < 0 or seconds < 0 or (n == 0 and seconds != 0):
                    raise ValueError("OSD counter reset/inconsistent sum/count")
                key = f"{osd}/{metric}"
                mean = 1000 * seconds / n if n else None
                means[key] = mean
                if metric == "op_latency":
                    self.stalled[osd] = self.stalled.get(osd, 0) + 1 if wip and not n else 0
                    if self.stalled[osd] >= 2:
                        raise ValueError(f"OSD {osd}: work in progress without completions")
                if mean is not None and mean >= 100:
                    raise ValueError(f"{key}: interval mean >=100 ms")
                if self.thresholds is None:
                    if mean is not None:
                        self.history[key].append(mean)
                else:
                    self.strikes[key] = (
                        self.strikes.get(key, 0) + 1
                        if mean is not None and mean > self.thresholds[key]
                        else 0
                    )
                    if self.strikes[key] >= 2:
                        raise ValueError(
                            f"{key}: two interval means above {self.thresholds[key]} ms"
                        )
        self.reference = sig
        self.first = start if self.first is None else self.first
        self.previous = copy.deepcopy(snapshot)
        return {"interval_mean_ms": means, "baseline_ready": self.thresholds is not None}

    def finish_baseline(self):
        if self.tripped is not None or not self.previous or self.first is None:
            raise ValueError("No healthy baseline")
        if self.previous["started"] - self.first < self.minimum_seconds:
            raise ValueError("Baseline too short")
        for osd in self.osds:
            if len(self.history[f"{osd}/op_latency"]) < self.minimum_intervals:
                if osd not in self.fixed_floor_osds:
                    raise ValueError(f"OSD {osd}: insufficient completed-operation intervals")
        limits = {}
        for key, values in self.history.items():
            ordered = sorted(values)
            p95 = ordered[math.ceil(len(ordered) * 0.95) - 1] if ordered else None
            # An inactive class has no measured baseline; use the fixed floor
            # when it becomes active, and retain None in the evidence.
            limits[key] = max(20, 3 * p95) if p95 is not None else 20
            if key.split("/", 1)[0] in self.fixed_floor_osds:
                if any(value > 20 for value in values):
                    raise ValueError(f"{key}: fixed-floor baseline already exceeds 20 ms")
                limits[key] = 20
            if limits[key] >= 100:
                raise ValueError(f"{key}: baseline too slow for hard 100-ms stop")
        self.thresholds = limits
        return {
            "threshold_ms": limits,
            "active_intervals": {key: len(value) for key, value in self.history.items()},
            "explicit_fixed_floor_osds": sorted(self.fixed_floor_osds),
        }

    def permit(self, now=None):
        """Call immediately before launching or renewing any endpoint lease."""
        now = time.monotonic() if now is None else now
        if self.tripped is not None or self.thresholds is None or self.previous is None:
            raise ValueError("Traffic requires a completed healthy baseline")
        if not 0 <= now - self.previous["started"] <= 10:
            self.trip("Ceph permit expired")


def supervise(guard, sample, renew, cancel, complete, record, period=5):
    """No renewal without a fresh passing read; cancel in every exit path.

    The endpoint lease independently expires within eight seconds if this
    controller blocks/dies. `cancel` must address only this batch's owned groups.
    The caller must obtain guard.permit() immediately before launching endpoints.
    """
    try:
        guard.permit()
        while not complete():
            started = time.monotonic()
            snapshot = sample()
            decision = guard.check(snapshot)
            guard.permit()
            record({"event": "ceph_decision", **decision})
            renew()
            time.sleep(max(0, period - (time.monotonic() - started)))
    except Exception as error:
        guard.tripped = str(error)
        record({"event": "ceph_stop", "reason": str(error)})
        raise
    finally:
        cancel()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--execute-read-only", action="store_true")
    args = parser.parse_args()
    if not args.execute_read_only:
        parser.error("--execute-read-only required; collector never launches traffic")
    config = json.loads(args.config.read_text())
    records = []
    snapshot = collect(config, records.append)
    print(json.dumps({"commands": records, "snapshot": snapshot}))


if __name__ == "__main__":
    main()
