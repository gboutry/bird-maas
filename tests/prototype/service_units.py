"""Owned systemd units; policy restoration precedes the BIRD workload."""

import re


def policy_command(owner: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9:_./-]+", owner):
        raise ValueError("Unsafe routing owner identifier")
    return f"/usr/bin/python3 /etc/maas-bird/policy_journal.py /etc/maas-bird {owner}"


def policy_unit(owner: str) -> str:
    return f"""[Unit]
Description=Restore owned MAAS BIRD policy
DefaultDependencies=no
After=local-fs.target
Before=network-pre.target
Wants=network-pre.target
[Service]
Type=oneshot
ExecStart={policy_command(owner)}
RemainAfterExit=yes
Environment=PATH=/usr/sbin:/usr/bin:/sbin:/bin
[Install]
WantedBy=multi-user.target
"""


def bird_unit(owner: str) -> str:
    return f"""[Unit]
Description=MAAS BIRD routing
After=network-online.target maas-bird-policy.service
Wants=network-online.target
Requires=maas-bird-policy.service
[Service]
Type=simple
User=bird
Group=bird
RuntimeDirectory=maas-bird
ExecStartPre=+{policy_command(owner)}
ExecStart=/usr/sbin/bird -f -c /etc/maas-bird/bird.conf -s /run/maas-bird/bird.ctl
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/etc/maas-bird
ProtectHome=true
PrivateTmp=true
ProtectKernelTunables=true
ProtectControlGroups=true
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW
[Install]
WantedBy=multi-user.target
"""
