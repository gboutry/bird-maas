"""Install an isolated OVN overlay on the owned MAAS workers and fabric VM.

OVN central stays in the fabric VM. Each chassis uses its real LXD UUID as
both its TLS identity and OVN system-id. Only workload veths and OVN tunnel
ports enter br-int; the routed host interfaces remain Linux interfaces.
"""

import argparse
import json
import os
import shlex
import subprocess

from commission import HOSTS, PROJECT, ROOT, command

DIRECTORY = "/etc/ovn/network-lab"
WORKERS = tuple(HOSTS)


def fabric(*args):
    return command("lxc", "exec", "fabric", "--project", PROJECT, "--", *args)


def ssh_args(name, *args):
    return [
        "ssh",
        "-F",
        str(ROOT / "runtime/juju-bootstrap-ssh.conf"),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "ubuntu@" + HOSTS[name]["management_ip"],
        shlex.join(args),
    ]


def ssh(name, *args, input=None, timeout=180):
    return subprocess.run(
        ssh_args(name, *args),
        input=input,
        text=True,
        check=True,
        capture_output=True,
        timeout=timeout,
    ).stdout.strip()


def owned():
    instances = {}
    for name in ("fabric", *WORKERS):
        instance = json.loads(command("lxc", "query", f"/1.0/instances/{name}?project={PROJECT}"))
        if (
            instance["type"] != "virtual-machine"
            or instance["config"].get("user.maas-bird-owner") != "network-lab"
            or instance["config"]["limits.cpu"] != "2"
            or instance["config"]["limits.memory"] != ("8GiB" if name == "fabric" else "4GiB")
        ):
            raise RuntimeError("OVN setup requires exclusively owned, capped lab VMs")
        instances[name] = instance["config"]["volatile.uuid"]
        if name != "fabric":
            native = ssh(name, "sudo", "cat", "/sys/class/dmi/id/product_uuid")
            if native.lower() != instances[name].lower():
                raise RuntimeError("Native worker UUID differs from LXD ownership record")
    return instances


def require_source_guards():
    for name in WORKERS:
        source = HOSTS[name]["loopback"].split("/")[0]
        addresses = json.loads(ssh(name, "ip", "-j", "-4", "address", "show", "dev", "lo"))
        if not any(
            a["local"] == source and a["prefixlen"] == 32 for a in addresses[0]["addr_info"]
        ):
            raise RuntimeError("Stable VTEP /32 is missing from the native loopback")
        rules = json.loads(ssh(name, "ip", "-j", "-4", "-N", "rule", "show"))
        selected = [r for r in rules if r["priority"] == 14000]
        if len(selected) != 1 or any(
            str(selected[0].get(key)) != value
            for key, value in (("src", source), ("table", "4400"), ("protocol", "4"))
        ):
            raise RuntimeError("Native VTEP source rule does not match the network owner policy")
        routes = json.loads(ssh(name, "ip", "-j", "-4", "-N", "route", "show", "table", "4400"))
        if not any(
            r.get("dst") == "default"
            and str(r.get("type")) == "7"
            and str(r.get("protocol")) == "4"
            and r.get("metric") == 42760
            for r in routes
        ):
            raise RuntimeError("The VTEP source table must retain its static unreachable guard")


def openssl(directory, *args):
    subprocess.run(["openssl", *args], cwd=directory, check=True, capture_output=True, timeout=60)


def certificates(directory, identities):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_mode & 0o077:
        raise ValueError("OVN certificate storage must be private")
    if not (directory / "ca.crt").exists():
        openssl(
            directory,
            "req",
            "-x509",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-days",
            "30",
            "-subj",
            "/CN=network-lab OVN CA",
            "-keyout",
            "ca.key",
            "-out",
            "ca.crt",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        )
    for name, identity in {"server": "network-lab-ovn", **identities}.items():
        if not (directory / f"{name}.crt").exists():
            openssl(
                directory,
                "req",
                "-new",
                "-newkey",
                "rsa:3072",
                "-nodes",
                "-subj",
                f"/CN={identity}",
                "-keyout",
                f"{name}.key",
                "-out",
                f"{name}.csr",
            )
            usage = "serverAuth" if name == "server" else "clientAuth"
            extensions = (
                "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                f"extendedKeyUsage={usage}\n"
            )
            if name == "server":
                extensions += "subjectAltName=IP:10.203.240.11\n"
            (directory / f"{name}.ext").write_text(extensions)
            openssl(
                directory,
                "x509",
                "-req",
                "-in",
                f"{name}.csr",
                "-CA",
                "ca.crt",
                "-CAkey",
                "ca.key",
                "-CAcreateserial",
                "-days",
                "30",
                "-extfile",
                f"{name}.ext",
                "-out",
                f"{name}.crt",
            )
        openssl(directory, "verify", "-CAfile", "ca.crt", f"{name}.crt")
        subject = subprocess.check_output(
            [
                "openssl",
                "x509",
                "-in",
                str(directory / f"{name}.crt"),
                "-noout",
                "-subject",
                "-nameopt",
                "RFC2253",
            ],
            text=True,
        ).strip()
        if subject != f"subject=CN={identity}":
            raise RuntimeError("Existing certificate identity differs; refuse silent reuse")


def install():
    fabric("env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "update", "-qq")
    fabric("env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "ovn-central")
    for name in WORKERS:
        ssh(
            name,
            "sudo",
            "debconf-set-selections",
            input="iperf3 iperf3/start_daemon boolean false\n",
        )
        ssh(name, "sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "update", "-qq")
        ssh(
            name,
            "sudo",
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            "apt-get",
            "install",
            "-y",
            "openvswitch-switch",
            "ovn-host",
            "iperf3",
        )


def central(directory):
    fabric("install", "-d", "-m", "700", DIRECTORY)
    for source, target in (
        ("ca.crt", "ca.crt"),
        ("server.crt", "cert.pem"),
        ("server.key", "key.pem"),
    ):
        command(
            "lxc",
            "file",
            "push",
            "--project",
            PROJECT,
            "--uid",
            "0",
            "--gid",
            "0",
            "--mode",
            "0600",
            str(directory / source),
            f"fabric{DIRECTORY}/{target}",
        )
    fabric(
        "ovn-sbctl",
        "set-ssl",
        DIRECTORY + "/key.pem",
        DIRECTORY + "/cert.pem",
        DIRECTORY + "/ca.crt",
    )
    # NB remains a local Unix socket; chassis receive only the restricted SB listener.
    fabric("ovn-sbctl", "set-connection", "role=ovn-controller", "pssl:6642:10.203.240.11")
    fabric("ovn-nbctl", "--may-exist", "ls-add", "network-lab-tenant")


def configure_ecmp():
    path = "/etc/sysctl.d/90-network-lab-ecmp.conf"
    for name in WORKERS:
        ssh(name, "sudo", "tee", path, input="net.ipv4.fib_multipath_hash_policy = 1\n")
        ssh(name, "sudo", "chmod", "644", path)
        ssh(name, "sudo", "sysctl", "-p", path)
        if ssh(name, "cat", "/proc/sys/net/ipv4/fib_multipath_hash_policy") != "1":
            raise RuntimeError("Native layer-4 ECMP policy did not take effect")


def chassis(name, identity, directory, index):
    ssh(name, "sudo", "install", "-d", "-m", "700", DIRECTORY)
    # ovs-ctl restores this persisted UUID into OVSDB at every service start.
    put_file(name, "/etc/openvswitch/system-id.conf", identity + "\n", "644")
    for source, target in (
        ("ca.crt", "ca.crt"),
        (f"{name}.crt", "cert.pem"),
        (f"{name}.key", "key.pem"),
    ):
        ssh(
            name,
            "sudo",
            "tee",
            DIRECTORY + "/" + target,
            input=(directory / source).read_text(),
        )
        ssh(name, "sudo", "chmod", "600", DIRECTORY + "/" + target)
    ssh(
        name,
        "sudo",
        "ovs-vsctl",
        "set-ssl",
        DIRECTORY + "/key.pem",
        DIRECTORY + "/cert.pem",
        DIRECTORY + "/ca.crt",
    )
    source = HOSTS[name]["loopback"].split("/")[0]
    ssh(
        name,
        "sudo",
        "ovs-vsctl",
        "set",
        "Open_vSwitch",
        ".",
        "external_ids:system-id=" + identity,
        "external_ids:hostname=" + name,
        "external_ids:ovn-remote=ssl:10.203.240.11:6642",
        "external_ids:ovn-encap-type=geneve",
        "external_ids:ovn-encap-ip=" + source,
        "external_ids:ovn-set-local-ip=true",
    )
    mac = f"02:00:00:ab:00:{20 + index:02x}"
    address = f"172.30.10.{20 + index}"
    port = "tenant-" + name
    fabric("ovn-nbctl", "--may-exist", "lsp-add", "network-lab-tenant", port)
    fabric("ovn-nbctl", "lsp-set-addresses", port, f"{mac} {address}")
    fabric("ovn-nbctl", "lsp-set-port-security", port, f"{mac} {address}")
    fabric("ovn-nbctl", "lsp-set-options", port, "requested-chassis=" + identity)
    return {"uuid": identity, "vtep": source, "mac": mac, "address": address, "port": port}


def put_file(name, path, content, mode):
    ssh(
        name,
        "sudo",
        "sh",
        "-c",
        'set -eu; temporary=$(mktemp -- "$1.XXXXXX"); '
        "trap 'rm -f -- \"$temporary\"' EXIT; "
        'cat > "$temporary"; chmod "$2" "$temporary"; mv -f -- "$temporary" "$1"',
        "network-lab-file",
        path,
        mode,
        input=content,
    )


def endpoint(name, config):
    ssh(name, "sudo", "install", "-d", "-m", "755", "/usr/local/libexec/network-lab")
    ssh(name, "sudo", "install", "-d", "-m", "755", "/etc/systemd/system/ovn-controller.service.d")
    ssh(name, "sudo", "install", "-d", "-m", "755", "/etc/systemd/system/bird.service.d")
    for source, target in (
        ("bird-network-lab-ovn.conf", "/etc/systemd/system/bird.service.d/network-lab-ovn.conf"),
        ("tenant_endpoint.py", "/usr/local/libexec/network-lab/tenant_endpoint.py"),
        ("network-lab-tenant.service", "/etc/systemd/system/network-lab-tenant.service"),
        (
            "ovn-controller-network-lab.conf",
            "/etc/systemd/system/ovn-controller.service.d/network-lab.conf",
        ),
    ):
        put_file(name, target, (ROOT / "ovn" / source).read_text(), "644")
    put_file(name, DIRECTORY + "/tenant.json", json.dumps(config, indent=2) + "\n", "600")
    ssh(name, "sudo", "systemctl", "daemon-reload")
    ssh(name, "sudo", "systemctl", "enable", "network-lab-tenant.service")
    ssh(name, "sudo", "systemctl", "restart", "network-lab-tenant.service")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--install", action="store_true", help="Install native Ubuntu packages first"
    )
    parser.add_argument(
        "--layer4-ecmp",
        action="store_true",
        help="Persist IPv4 layer-4 hashing for per-flow Geneve ECMP on owned workers",
    )
    args = parser.parse_args()
    instances = owned()
    if args.install:
        install()
    require_source_guards()
    if args.layer4_ecmp:
        configure_ecmp()
    directory = ROOT / "runtime/ovn/tls"
    certificates(directory, {name: instances[name] for name in WORKERS})
    central(directory)
    manifest_path = directory.parent / "manifest.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest = {}
    for index, name in enumerate(WORKERS):
        manifest[name] = chassis(name, instances[name], directory, index)
        if previous.get(name, {}).get("gateway"):
            if any(
                previous[name][k] != manifest[name][k] for k in ("uuid", "address", "vtep", "mac")
            ):
                raise RuntimeError(
                    "Existing tenant gateway belongs to a different host allocation"
                )
            manifest[name]["gateway"] = previous[name]["gateway"]
        endpoint(name, manifest[name])
        print(name + ": native OVN chassis and tenant endpoint configured", flush=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
