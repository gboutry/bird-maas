"""Live TLS and restricted-project checks for the native MAAS power probe."""

import json
import subprocess


def controller(*args):
    return subprocess.check_output(
        ["lxc", "exec", "--project", "maas-bird-network-lab", "maas", "--", *args],
        text=True,
        timeout=60,
    )


CURL = (
    "curl --silent --show-error --cacert /etc/nginx/lxd-power/server.crt "
    "--cert /etc/nginx/lxd-power/client.crt --key /etc/nginx/lxd-power/client.key "
)


def test_native_maas_commissioned_same_vm():
    nodes = json.loads(controller("maas", "lab", "machines", "read"))
    matches = [node for node in nodes if node["hostname"] == "tf-power-probe"]
    assert len(matches) == 1
    node = matches[0]
    instance = json.loads(
        subprocess.check_output(
            [
                "lxc",
                "query",
                "/1.0/instances/tf-power-probe?project=maas-bird-tf-probe",
            ],
            text=True,
            timeout=30,
        )
    )
    assert node["hostname"] == instance["name"]
    assert node["hardware_uuid"] == instance["config"]["volatile.uuid"]
    assert node["status_name"] == "Ready"
    assert node["cpu_count"] == 1
    assert node["memory"] == 2048
    assert node["min_hwe_kernel"] == "hwe-24.04"


def test_legacy_endpoint_and_restricted_project():
    value = json.loads(
        controller(
            "sh",
            "-ceu",
            CURL
            + "'https://127.0.0.1:944/1.0/virtual-machines/tf-power-probe?project=maas-bird-tf-probe'",
        )
    )
    assert value["metadata"]["name"] == "tf-power-probe"
    assert value["metadata"]["type"] == "virtual-machine"
    denied = controller(
        "sh",
        "-ceu",
        CURL
        + "-o /dev/null -w '%{http_code}' 'https://127.0.0.1:944/1.0/instances/maas?project=maas-bird-network-lab'",
    )
    assert denied == "403"
    unauthenticated = controller(
        "curl",
        "--silent",
        "--show-error",
        "--cacert",
        "/etc/nginx/lxd-power/server.crt",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "https://127.0.0.1:944/1.0",
    )
    assert unauthenticated == "400"


def test_backend_tls_rejects_wrong_ca_and_recovers():
    result = controller(
        "sh",
        "-ceu",
        """
        saved=$(mktemp)
        cp /etc/nginx/lxd-power/host.crt "$saved"
        restore() {
          cp "$saved" /etc/nginx/lxd-power/host.crt
          chmod 0600 /etc/nginx/lxd-power/host.crt
          nginx -t >/dev/null 2>&1
          systemctl restart nginx
          rm -f "$saved"
        }
        trap restore EXIT
        cp /etc/nginx/lxd-power/server.crt /etc/nginx/lxd-power/host.crt
        nginx -t >/dev/null 2>&1
        systemctl restart nginx
        """
        + CURL
        + "-o /dev/null -w '%{http_code}' 'https://127.0.0.1:944/1.0?project=maas-bird-tf-probe'",
    )
    assert result == "502"
    recovered = controller(
        "sh",
        "-ceu",
        CURL
        + "-o /dev/null -w '%{http_code}' 'https://127.0.0.1:944/1.0?project=maas-bird-tf-probe'",
    )
    assert recovered == "200"
