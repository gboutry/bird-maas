"""Run inside the owned MAAS guest after cloud-init completes.

Credentials remain root-readable inside the guest, outside Terraform and Git.
This lab uses the local API only; the charm integration will require HTTPS.
"""

import argparse
import os
import secrets
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tls", action="store_true", help="Use native TLS after trusting the lab CA"
    )
    args = parser.parse_args()
    os.umask(0o077)
    private = Path("/root/network-lab")
    private.mkdir(mode=0o700, exist_ok=True)
    password = private / "admin-password"
    if not password.exists():
        password.write_text(secrets.token_urlsafe(32))
    lookup = subprocess.run(
        ["maas", "apikey", "--username=lab"], text=True, capture_output=True, timeout=60
    )
    if lookup.returncode:
        subprocess.run(
            [
                "maas",
                "createadmin",
                "--username=lab",
                "--email=lab@example.invalid",
                "--password=" + password.read_text(),
            ],
            check=True,
            timeout=120,
        )
        key = subprocess.check_output(
            ["maas", "apikey", "--username=lab"], text=True, timeout=60
        ).strip()
    else:
        key = lookup.stdout.strip()
    subprocess.run(
        [
            "maas",
            "login",
            "lab",
            "https://127.0.0.1:5443/MAAS" if args.tls else "http://127.0.0.1:5240/MAAS",
            key,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        timeout=60,
    )
    print("Local lab CLI profile ready; credentials retained only in the guest")


if __name__ == "__main__":
    main()
