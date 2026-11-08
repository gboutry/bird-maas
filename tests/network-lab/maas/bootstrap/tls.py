"""Enable native MAAS TLS in the disposable guest with a private lab CA."""

import os
import shutil
import subprocess
from pathlib import Path


def run(*args):
    subprocess.run(args, check=True, timeout=120)


def main():
    os.umask(0o077)
    directory = Path("/var/snap/maas/common/network-lab-tls")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chdir(directory)
    if not Path("ca.crt").exists():
        run(
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-days",
            "30",
            "-subj",
            "/CN=maas-bird disposable lab CA",
            "-keyout",
            "ca.key",
            "-out",
            "ca.crt",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        )
    if not Path("server.crt").exists():
        run(
            "openssl",
            "req",
            "-new",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-subj",
            "/CN=maas.lab.invalid",
            "-keyout",
            "server.key",
            "-out",
            "server.csr",
        )
        Path("server.ext").write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=IP:10.203.240.10,IP:127.0.0.1,DNS:maas.lab.invalid\n"
        )
        run(
            "openssl",
            "x509",
            "-req",
            "-in",
            "server.csr",
            "-CA",
            "ca.crt",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-days",
            "30",
            "-extfile",
            "server.ext",
            "-out",
            "server.crt",
        )
    run("openssl", "verify", "-CAfile", "ca.crt", "server.crt")
    run(
        "maas",
        "config-tls",
        "enable",
        "--yes",
        "--cacert",
        str(directory / "ca.crt"),
        str(directory / "server.key"),
        str(directory / "server.crt"),
    )
    shutil.copyfile(directory / "ca.crt", "/usr/local/share/ca-certificates/network-lab-maas.crt")
    run("update-ca-certificates")
    print("MAAS TLS configured on 5443; verify the live endpoint with this CA")


if __name__ == "__main__":
    main()
