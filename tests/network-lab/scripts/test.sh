#!/usr/bin/env bash
# Run native acceptance groups serially and retain every group's exit status.
set -euo pipefail
umask 077
lab_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
repo_dir=$(cd -- "$lab_dir/../.." && pwd)
suite=baseline
usage() {
    cat <<'HELP'
Usage: test.sh [--suite model|baseline|process|bfd|blackhole|tor|evpn|faults|all]

model: local model and monitor tests only.
baseline: live four-worker MAAS/Juju/BIRD/OVN/provider acceptance, including
          strict cloud-init and Geneve-distribution assertions.
process: live native BIRD and Juju-agent restart checks only.
bfd: live BFD-only UDP-drop checks with link and neighbor forwarding retained.
blackhole: detect real forwarding loss while BGP/BFD remain up.
tor: all four ToR data-port outages, with continuous VTEP and NAT probes.
evpn: each ToR EVPN AF peer withdrawal with provider/NAT probes.
faults: live disruptive lab-only fault and fabric restart/reconfigure cases.
all: baseline followed by faults.

Groups run serially. Failures remain failures; later groups still run.
JUnit XML, command logs, resource samples and statuses stay under runtime/runs.
This runner covers implemented cases; it does not imply the entire failure
matrix or fresh bootstrap/deploy/cleanup lifecycle has been validated.
HELP
}
while (($#)); do
    case "$1" in
        --suite) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; suite=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
case "$suite" in model|baseline|process|bfd|blackhole|tor|evpn|faults|all) ;; *) usage >&2; exit 2 ;; esac
cd -- "$repo_dir"
mkdir -p "$lab_dir/runtime/runs"
run_dir=$(mktemp -d "$lab_dir/runtime/runs/${suite}-XXXXXXXX")
monitor_pid=
finish() {
    if [[ -n "$monitor_pid" ]]; then
        kill "$monitor_pid" 2>/dev/null || true
        wait "$monitor_pid" 2>/dev/null || true
    fi
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ "$suite" != model ]]; then
    uv run --locked python "$lab_dir/scripts/monitor.py" \
        --output "$run_dir/resources.jsonl" > "$run_dir/monitor.log" 2>&1 &
    monitor_pid=$!
fi
result=0
run_group() {
    local group=$1 status
    shift
    printf '\nRunning %s\n' "$group"
    if taskset -c 0,1 uv run --locked pytest -q --junitxml="$run_dir/$group.xml" \
        "$@" 2>&1 | tee "$run_dir/$group.log"; then
        status=0
    else
        status=$?
        result=1
    fi
    printf '%s\t%s\n' "$group" "$status" >> "$run_dir/status.tsv"
    if [[ -n "$monitor_pid" ]] && ! kill -0 "$monitor_pid" 2>/dev/null; then
        printf 'Resource monitor exited; refusing further live checks.\n' >&2
        exit 1
    fi
}
if [[ "$suite" == model || "$suite" == baseline || "$suite" == all ]]; then
    run_group model "$lab_dir/test_network_model.py" "$lab_dir/test_monitor.py" \
        "$lab_dir/test_seed_prerequisites.py" "$lab_dir/test_deployment_targets.py" \
        "$lab_dir/test_controller_registration.py" "$lab_dir/test_cleanup.py" \
        "$lab_dir/test_fabric_model.py" "$lab_dir/test_rack_tags.py" \
        "$lab_dir/test_provider_readiness.py"
fi
if [[ "$suite" == baseline || "$suite" == all ]]; then
    run_group fabric "$lab_dir/tests/routing/test_fabric_preflight.py" \
        "$lab_dir/tests/routing/test_frr_underlay.py" --host-routing
    run_group maas "$lab_dir/tests/maas/test_roles.py" \
        "$lab_dir/tests/maas/test_broadcast_isolation.py" "$lab_dir/tests/geneve/test_hwe_boot.py"
    run_group juju "$lab_dir/tests/juju/test_native_inventory.py" \
        "$lab_dir/tests/juju/test_host_routing.py" \
        "$lab_dir/tests/juju/test_activation_preflight.py"
    run_group storage "$lab_dir/tests/ceph/test_host_roles.py" \
        -k 'not both_fast and not one_fast and not one_gp_child'
    run_group overlay "$lab_dir/tests/geneve/test_ovn_overlay.py" \
        "$lab_dir/tests/geneve/test_flow_distribution.py"
    run_group provider "$lab_dir/tests/provider/test_evpn_transport.py" \
        "$lab_dir/tests/provider/test_ovn_gateways.py"
fi
if [[ "$suite" == process || "$suite" == faults || "$suite" == all ]]; then
    run_group process-faults "$lab_dir/tests/juju/test_process_recovery.py" -k "native_bird or native_juju_agent"
fi
if [[ "$suite" == bfd || "$suite" == faults || "$suite" == all ]]; then
    run_group bfd-faults "$lab_dir/tests/juju/test_process_recovery.py" -k bfd_only
fi
if [[ "$suite" == blackhole || "$suite" == faults || "$suite" == all ]]; then
    run_group forwarding-blackholes "$lab_dir/tests/juju/test_process_recovery.py" \
        -k forwarding_blackhole
fi
if [[ "$suite" == tor || "$suite" == faults || "$suite" == all ]]; then
    run_group tor-faults "$lab_dir/tests/routing/test_tor_failover.py" --host-routing
fi
if [[ "$suite" == evpn || "$suite" == faults || "$suite" == all ]]; then
    run_group evpn-faults "$lab_dir/tests/provider/test_evpn_failover.py"
fi
if [[ "$suite" == faults || "$suite" == all ]]; then
    run_group host-faults "$lab_dir/tests/ceph/test_host_roles.py" \
        -k 'both_fast or one_fast or one_gp_child'
    run_group attachment-faults "$lab_dir/tests/juju/test_attachment_failures.py"
    run_group fabric-faults "$lab_dir/tests/routing/test_frr_failover.py" --host-routing
    run_group overlay-faults "$lab_dir/tests/geneve/test_ovn_failover.py"
    run_group gateway-faults "$lab_dir/tests/provider/test_gateway_failover.py"
    run_group fabric-lifecycle "$lab_dir/tests/provider/test_provider_recovery.py"
    run_group gateway-after-recovery "$lab_dir/tests/provider/test_ovn_gateways.py"
fi
printf '\nResults: %s\n' "$run_dir"
exit "$result"
