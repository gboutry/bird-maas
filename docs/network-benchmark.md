# PF/VF Linux VLAN benchmarks

`scripts/network_benchmark.py` captures machine state and runs kernel TCP/UDP
benchmarks through **existing** PF or VF VLAN interfaces. It does not create
VLANs, change routes, offloads, IRQ placement or CPU policy, configure a DPU,
bond VFs, install dependencies, or inject faults. Its agent is sent as Python
source over SSH, with no remote script installation. Python 3.10 or later is
required remotely; use the project's Python environment for the controller.

The manifest describes two host endpoints and their DPU Arm observers. Add
both Arms per host when both DPUs participate; the sample includes just one
DPU per host for the smallest single-NUMA test. UUID checks bind host access
to DMI identity; `identity_kind: machine-id` supports Arms without DMI UUIDs.
SSH host keys must already be trusted. There is no automatic host-key acceptance
or privilege escalation. State collection can retain permission failures, but
benchmark preflight and execution failures stop the run.

## Prepare the manifest without contacting machines

Copy `examples/network-benchmark/manifest.json` to a private location and replace
all placeholder identities, names, PCI BDFs, VLANs, addresses and CPU IDs. The
example rates are illustrative offered loads, not measured hardware limits or
recommended rates for an unknown link speed. Capture output contains machine
identities, network topology and process mappings; evidence directories are
created with owner-only access.

```sh
uv run --locked python scripts/network_benchmark.py plan /private/benchmark.json
```

`validate` accepts an inventory-only manifest and does not invoke SSH:

```sh
python3 scripts/network_benchmark.py validate /private/inventory.json
```

`plan` does not invoke SSH. Other commands require `--execute`. These commands
are instructions for later authorized lab use; local developer tests do not
invoke them against hardware.

The PF and VF modes must carry the same IPv4 addresses, VLAN and networks,
using separate Linux VLAN interfaces over the corresponding PCI functions.
Run each mode only when that mode is already configured. Do not assign the
same addresses to both modes simultaneously. The runner checks the selected
route and binds both iperf endpoints to the selected VLAN device. It rejects
bond parents, a missing VLAN interface, wrong VLAN/MTU/PCI/PF-VF identity,
wrong NUMA placement, unavailable CPU/memory binding and occupied TCP ports.
It never switches the machine between modes.

CPU IDs in each endpoint placement must belong to its specified memory NUMA
node. The interface NUMA node is separately checked against sysfs. Choose
matching nodes for a local case, or a different CPU/memory node at one endpoint
for a cross-NUMA case. This version moves CPU and memory together; it does not
independently sweep CPU and memory locality. IRQ/queue placement remains an
operator-provided invariant, captured as evidence.

Keep the DPU's physical LACP arrangement and ToR path the same between modes.
Use endpoints on different servers, through the physical fabric; avoid a
same-DPU shortcut. PF access must remain in the same supported DPU mode and
eSwitch path for an isolated attachment comparison. Otherwise describe the
result as a comparison of complete configurations. Record active LACP members,
link rates and per-member counters; multiple flows do not guarantee balanced
hashing. Management links must never carry benchmark data.

## Capture state

For inventory before attachment details are known, copy
`examples/network-benchmark/inventory.json`. This needs only node identities
and SSH targets, with no benchmark configuration or existing PF/VF VLANs.
Omitting `interfaces` discovers all current network interfaces. Add the other
host and Arms to that inventory as needed. The full benchmark manifest can
also be used for capture with its explicit interface selection.

```sh
uv run --locked python scripts/network_benchmark.py capture /private/inventory.json \
  --execute --output /private/evidence/inventory
```

All listed nodes are observed, including the DPU Arms. Capture records kernel,
CPU/NUMA topology and frequency policy, BIOS, PCI devices, driver/firmware,
links/addresses/routes/rules, VLANs, MTUs, queue/ring/coalescing/RSS/offload state,
IRQ affinity, LACP bond readback, devlink/eSwitch and OVS/offload information.
Every command retains its arguments, timestamps, stdout, stderr and exit code.
Missing tools, unsupported queries and permission failures remain explicit.
Capture does not establish that the current configuration meets the design.

## Run an already configured mode

Endpoints need `iperf3`, `numactl`, `ip`, `ss` and iputils `ping`. The selected
account needs permission to bind sockets to devices and memory to NUMA nodes.
Use matching iperf3 builds supporting `--bind-dev`, `--bidir`, `--dont-fragment`,
JSON and server output retrieval. The script does not grant capabilities or
change privileges. See the [iperf3 manual](https://software.es.net/iperf/invoking.html)
for option semantics and platform restrictions. `ethtool`, `lspci`, `devlink`
and OVS tools improve state capture; unavailable queries are recorded.

```sh
uv run --locked python scripts/network_benchmark.py run /private/benchmark.json \
  --mode pf --execute --output /private/evidence/pf
# After a separately authorized operator change to the VF attachment:
uv run --locked python scripts/network_benchmark.py run /private/benchmark.json \
  --mode vf --execute --output /private/evidence/vf
```

Each trial checks the endpoints, estimates each node's clock offset and its
round-trip uncertainty, captures before-state, starts all samplers, measures an
idle baseline, starts a one-shot server, and runs the client with an omitted
warm-up interval. It retains both endpoint results, concurrent telemetry and
after-state. The server must own the listening socket before readiness is
accepted. Only owned processes are stopped; there is no `pkill`, port eviction
or remote reconfiguration. Remote operations have bounded lifetimes even if the
controller disconnects. Samplers stream directly into evidence files.

`cases` is an explicit small matrix rather than an automatic Cartesian product:

- `direction`: `a-b`, `b-a`, or simultaneous `bidir`.
- `transport`: kernel `tcp` or `udp`, with fixed `flows` and `placement`.
- Integer `rate_bps`: aggregate application-payload offered rate **per direction**,
  divided across iperf streams. It is not Ethernet wire rate.
- TCP `rate_bps: maximum`: unpaced TCP; report achieved throughput and endpoint
  costs. It is not a proof of the fabric's maximum throughput.
- UDP `payload_bytes`: explicit datagram payload. With IPv4 MTU 9000, 8972
  fills the IP MTU. Payload 18 produces a 68-byte single-tag Ethernet frame
  including FCS (64 without the tag). This excludes preamble and inter-frame gap.
  TCP uses normal MSS/segmentation; UDP payload sizes do not define TCP packets.

For a bounded UDP zero-loss capacity search, replace an integer rate in a UDP
case with:

```json
"rate_bps": "search",
"search": {"min_bps": 8000000, "max_bps": 8000000000, "steps": 6}
```

Bounds must divide evenly across `flows`. Every candidate runs the same
warm-up/duration and preserves a complete trial. The search tests the configured
ceiling first, then bisects; it reports the best **observed** zero-loss rate,
search resolution/status, and all attempts. Missing loss counters or a sender
achieving less than 99% of the requested payload rate make the search
inconclusive. The latter identifies a source/rate-generation limitation, not
its cause. No tail-latency acceptance threshold is imposed by this search;
review the latency results separately. Repeats rerun the entire search.

The offline plan alternates preferred PF/VF order across repeats. A `run`
executes only the selected mode and cannot alternate attachments for you.
For an interleaved campaign, use one-repeat manifests with identical contents
for each paired PF/VF batch, alternate the operator's mode order between batches,
and keep batch evidence separate. Start with the supplied four traffic cases,
then add the reverse/bidirectional, one-flow and cross-NUMA counterparts needed
for a specific question. Keep firmware, offloads, MTU, frequency policy,
queue/IRQ/core placement and competing workload fixed. Review captured settings
before interpreting a difference; the tool does not certify their equivalence.

## Evidence and comparison

Every run has a manifest and source hashes. Every trial has identity/route
preflight evidence, clock estimates, before/after state, raw client and server
JSON, ping output and one-second JSONL telemetry on all listed nodes. Samplers
record per-core CPU, IRQ/softirq and network counters, softnet drops, system
memory, NUMA counters and selected process CPU/RSS/NUMA mappings.

The sampler also reads `ethtool -S` and `tc -s -j qdisc show` for each selected
interface. Driver counters retain their native names, including per-RX/TX-queue
packets, allocation failures, no-buffer events, steering misses, drops and errors
where exported. Driver query failures stay explicit; VLAN interfaces can lack
hardware statistics while their parent PF has them. Select only the relevant
parent/VF, VLAN, representor, bond and physical uplinks in the manifest rather
than sampling every VF in the inventory.

`ss -tinmH` and `ss -uanmH` retain socket memory, queue/backlog and TCP details
for the benchmark port, on both endpoints and any listed observers. A separate
TCP socket query covers the observed Ceph port range 6800–7300; deployments using
other ports require a different selector. `/proc/net/snmp`, `netstat`, `snmp6`
and `sockstat6` retain protocol and socket-memory evidence, including TCP
retransmissions, backlog drops, memory pressure and UDP receive/send buffer
errors. Paired protocol headers are parsed by field name; known gauges such as
current established connections are kept separate from cumulative counters.
Buffer limits and TCP/UDP memory settings are captured before/after without
writing sysctls. Ceph OSD processes and the observer's own PID are also recorded.

The requested period is one second. Each read command has a two-second timeout
and at most four command readers run concurrently. Actual collection time,
the previous sample-start gap and overruns are recorded; query failure or a slow
SSH stream must not be mistaken for one-second coverage. IRQ samples use a
sparse representation of all CPU columns: an omitted CPU count in a row means
exactly zero, all IRQ rows remain present, and global lines remain verbatim.
Before/after state capture retains the full original `/proc/interrupts` text.
This avoids repeatedly transmitting hundreds of zero columns on large hosts.

Counter deltas retain new/disappearing/decreased fields. A failed read breaks
continuity, so the next successful read is not treated as an interval delta.
Decreases can represent resets or gauge behaviour; interpret vendor counters
using their owning definitions. In particular, steering misses are distinct
from receive-buffer exhaustion. Interface, kernel, per-queue and hardware
counters overlap and include production traffic; do not sum them as benchmark
packet loss or attribute background changes to a test without evidence.
[Linux mlx5 counters](https://docs.kernel.org/6.17/networking/device_drivers/ethernet/mellanox/mlx5/counters.html)
and [SNMP counter documentation](https://docs.kernel.org/networking/snmp_counter.html)
describe these boundaries. Configured ring size, hardware queue occupancy,
qdisc backlog and socket memory are different measurements. Generic hardware
ring occupancy remains explicitly unavailable.

`summary.json` retains iperf end statistics (delivery, UDP loss/jitter, TCP
retransmits and iperf process CPU where emitted). It derives sampled ICMP RTT
percentiles, per-node/per-core busy CPU, IRQ/softirq CPU time, idle versus loaded
CPU, peak observed process RSS, and total/incremental CPU-seconds per delivered
Gbit. UDP also gets CPU-seconds per delivered million packets where receiver
packet counters are available. Host and Arm costs remain separate. Idle polling
is included in total cost; subtraction is shown separately and may be negative
when background activity varies. CPU includes other work on the machine and
collection overhead, not just the benchmark process.

Per-node idle/loaded network summaries include interval counter changes,
discontinuities, query failures and collector timing. Detailed socket and qdisc
snapshots stay in the raw JSONL. The workload report does not turn missing
telemetry into zero or treat a phase spanning a counter reset as a complete
measurement.

The CPU windows exclude one second at each load boundary and use sampled clock
offset correction. Offset uncertainty is recorded; preflight rejects uncertainty
over one second. This is not PTP synchronization or a one-way latency clock.
All raw wall/monotonic timestamps remain available. Very short phases can lack
enough samples; their cost is marked unavailable rather than zero.

```sh
uv run --locked python scripts/network_benchmark.py report \
  /private/evidence/pf /private/evidence/vf --output /private/evidence/comparison
node /path/to/benchmark-report/bin/report.mjs \
  /private/evidence/comparison/report.json /private/evidence/comparison/report.html
```

Comparison requires identical manifests. It combines raw trial summaries and
emits the existing `benchmark-report` renderer's input schema; it does not
silently average repeats or select a winner. Inspect individual repeats and
spread. Output directories/files are never overwritten.

## Verification boundary and remaining measurements

The developer tests execute the actual controller CLI with fixture SSH agents,
and exercise agent preflight using fixture sysfs/command observations. They
check validation, binding/rate construction, streaming, cleanup, failed trials,
search decisions and CPU accounting. They do not contact hardware, open live
benchmark sockets or claim Linux/driver integration acceptance.

The implemented traffic suite is kernel iperf3 plus ICMP probes. ICMP RTT is
neither Ceph/OVN request latency nor the latency distribution of every data
packet. At five probes per second, a 120-second run provides too few observations
for a strong p99.9 estimate; retain counts and raw samples. Blocked ICMP is
reported separately from throughput failure. Memory bandwidth/PCIe/inter-socket
traffic require platform-specific PMU tools and are explicitly unavailable here;
NUMA placement and software counters alone do not measure memory bandwidth.

TRex is not invoked by this suite. A separate generator calibration is needed
when the NIC/DPDK support and generator ports are known. TRex results would
measure a different endpoint datapath and must not substitute for these kernel
costs. The runner also does not provision or benchmark Ceph, OVN tenant paths,
flow-installation churn, storage or failures. Those need workload-specific,
separately agreed integration cases; synthetic packets do not establish their
acceptance. Hardware acceptance must first confirm interface binding, actual
wire paths, MTU, offloads, counter semantics, collector overhead and cleanup
on the installed software.

## Portable archive

Build an archive containing the scripts, inventory/benchmark templates, license
and a quickstart guide using only the Python standard library:

```sh
python3 scripts/pack_network_benchmark.py --output build/network-benchmark.tar.gz
```

To include the existing dependency-free Node.js report renderer, add
`--renderer-source /path/to/benchmark-report`. The builder reads only an explicit
source allowlist, includes SHA256 integrity records, produces deterministic
archive bytes and refuses to overwrite an archive. It does not bundle credentials,
execution evidence, Python packages or architecture-specific system executables.
Extract and follow the archive's `README.md`; no pip, uv or npm is required.
# Production Ceph guard helpers

`scripts/network_benchmark_ceph.py` is a standard-library, read-only collector
and policy module. Its collector runs on a bastion with existing SSH access to
the monitor and OSD hosts. It checks machine IDs and reads monitor status,
health detail and OSD tree, plus local OSD admin-socket counters and systemd
process identities. Raw command intents, results and timing are retained.
It never changes Ceph flags, archives crashes or generates benchmark traffic.
The collector CLI requires `--execute-read-only`.

The policy requires a five-minute baseline, checks every five seconds, rejects
snapshots older than ten seconds, and latches failures. Accepted health detail
must be supplied explicitly; an existing warning is not a blanket exception
for future warnings. Quorum, OSD membership/process identities, PG states and
pool counts must remain stable. All PGs must remain active+clean.

Latency uses deltas of completed-operation sum/count counters for aggregate,
read, write, mixed and replication sub-operations. No completions means an
unavailable interval mean, never zero latency. Stop on an interval mean of
100 ms or greater, or two consecutive intervals above the larger of 20 ms
and three times the baseline p95 of interval means. These are not client
request percentiles. Work in progress without any aggregate completions for
two polls also trips the guard. Counter resets and required-read failures
trip immediately.

By default each target needs at least ten active aggregate intervals.
An explicitly approved `fixed_floor_osds` list can name sparsely active OSDs
that instead use the fixed 20 ms limit, with no relative-baseline claim.
Their observed baseline means must stay below that limit. Missing counters
are never eligible for this exception.

`scripts/network_benchmark_lease.py` owns a new process group for one supplied
command. Renew its stdin lease only after a passing fresh guard decision.
EOF, cancellation, malformed input, an eight-second maximum lease or a
92-second maximum watchdog stops that group with SIGTERM, followed by
SIGKILL after at most one second. It does not kill unrelated processes.
Use an independent bounded `timeout` around traffic as protection against
the lease agent itself being killed. Local tests use sleeping Python children
and generate no network traffic.

`supervise()` connects guard decisions to lease renewals and always cancels on
exit. A campaign driver must call `permit()` before launching either endpoint
and use leased commands for both endpoints. The generic `run` command does
not automatically use these helpers and is not a production Ceph safeguard.
Campaign integration and each traffic batch require explicit authorization.
