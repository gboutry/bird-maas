"""Small owned-daemon primitives; the sole supported lifecycle is RackService."""

import grp
import os
import re
import subprocess
from pathlib import Path


class RuntimeErrorBase(RuntimeError):
    pass


class SessionsPending(RuntimeErrorBase):
    pass


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeErrorBase(f"Workload command failed: {args[0]}") from exc
    if check and result.returncode:
        raise RuntimeErrorBase(f"Workload command failed: {args[0]}")
    return result


def write_atomic(path: Path, contents: str, mode: int, group: int | None = None):
    """Leave either the old complete file or the new one after an interrupted write."""
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        if group is not None:
            os.chown(temporary, 0, group)
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


class OwnedBird:
    def __init__(self, root: Path = Path("/"), owner: str = "development"):
        self.owner = owner
        self.root = root
        self.directory = root / "etc/maas-bird"
        self.config = self.directory / "bird.conf"
        self.marker = self.directory / "owner"
        self.unit = root / "etc/systemd/system/maas-bird.service"
        self.socket = "/run/maas-bird/bird.ctl"
        self.policy_unit = self.unit.with_name("maas-bird-policy.service")

    def _control(self, *args, check=True):
        return run(["birdc", "-s", self.socket, *args], check=check)

    def _active(self):
        return run(["systemctl", "is-active", "maas-bird.service"], check=False).returncode == 0

    def _start(self):
        # A newly installed inactive unit can have no loaded failure record;
        # reset-failed then fails even though enable --now can start it normally.
        failed = run(["systemctl", "is-failed", "--quiet", "maas-bird.service"], check=False)
        if failed.returncode == 0:
            run(["systemctl", "reset-failed", "maas-bird.service"])
        elif failed.returncode != 1:
            raise RuntimeErrorBase("Cannot inspect owned service failure state")
        run(["systemctl", "enable", "--now", "maas-bird.service"])

    def _write_config(self, path, contents):
        group = grp.getgrnam("bird").gr_gid
        os.chown(self.directory, 0, group)
        write_atomic(path, contents, 0o640, group)

    def stop(self):
        if self.marker.exists() and self.marker.read_text() == self.owner + "\n":
            run(["systemctl", "disable", "--now", "maas-bird.service"])

    def invalidate(self):
        """Remove durable startup material before attempting process stop."""
        if not self.marker.exists() or self.marker.read_text() != self.owner + "\n":
            return
        for name in ("bird.conf", "candidate.conf", "activation.json"):
            (self.directory / name).unlink(missing_ok=True)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def accepted(protocols: str, bfd: str, count: int) -> bool:
    """Verify actual BGP, IPv4 extended-next-hop capability and BFD, including on BIRD 2.14."""
    sections = re.split(
        r"(?m)^(?=[A-Za-z_][A-Za-z0-9_]*\s+(?:BGP|Pipe|Kernel|Direct|Device|Static)\s)",
        protocols,
    )
    blocks = [section for section in sections if re.match(r"uplink_[0-9]+\s+BGP\s", section)]
    if len(blocks) != count:
        return False
    for block in blocks:
        if "Established" not in block.splitlines()[0]:
            return False
        if "Neighbor capabilities" not in block:
            return False
        neighbor = block.split("Neighbor capabilities", 1)[1].split("Session:", 1)[0]
        if "Extended next hop" not in neighbor or not re.search(
            r"IPv6 nexthop:\s+ipv4(?:\s|$)", neighbor
        ):
            return False
        if not re.search(r"Channel ipv4\s+State:\s+UP", block):
            return False
    rows = [line.split() for line in bfd.splitlines() if line.strip().startswith("fe80:")]
    return len(rows) == count and all("Up" in row for row in rows)
