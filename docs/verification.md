# Verification boundaries

Do not promote a targeted passing run into a full-suite result. Record the exact
source revision, command, exit status, native software version and fixture cleanup
for each acceptance run. Keep deployment receipts and captured inventory outside
the public source tree. Rerun artifact verification after changing shared or
charm sources.

| Layer | Native command | What it establishes |
|---|---|---|
| Types | `make typecheck` | Static type checks only |
| Style | `make lint` | Repository-native Ruff checks |
| Components | `make test` | Ops event handling, compiler, authority, property and fault-model tests |
| BIRD | `BIRD_BINARY=... make native` | Executed parser and native daemon control behavior |
| Network namespaces | `BIRD_BINARY=... make namespaces` | Real Linux policy lookup, unnumbered BGP/BFD and packets in disposable namespaces |
| systemd | `uv run pytest tests/systemd` | Installed helper, permissions, accepted startup/restart and missing-guard refusal in an owned LXD virtual machine |
| Juju | `uv run pytest tests/integration --juju-controller <isolated-controller>` | Packed principal deployment, actual hooks/secrets/HTTPS and rack relation delivery |
| Build | `uv run python scripts/pack.py --development-charmcraft --clean` | Native experimental-monorepo Charmcraft packing |
| Artifact | `uv run python scripts/verify_artifacts.py` | Exact shared/charm source and metadata match; no Hypothesis or retired prototypes |

## Test interpretation

Production tests use the supported rack compiler/runtime. Historical reproductions
import isolated implementations under `tests/prototype`; their passing totals do
not establish correctness of the supported implementation. Those implementations
are excluded from shipped artifacts.

Property tests state the invariant and why generated variation matters. Current
examples include rack-slice isolation at up to 60 by 40 hosts, order-independent
rendering, and monotonic/idempotent authorization narrowing. These are content and
component properties, not a deployment-scale benchmark.

Namespace scenarios test distinct public and replication contexts at 50 and 300 ms
BFD intervals, source-wide leak prevention, silent loss, all-path failure and peer
withdrawal without resetting surviving sessions. They do not measure ASIC offload,
physical NUMA transport locality or a Ceph latency objective. Command-model tests
explicitly double systemd and peer health and must not be reported as live-daemon
acceptance.

The Juju HTTPS inventory fixture is synthetic. Its deliberately nonmatching host
UUIDs must not authorize routing on LXD guests. It proves transport, secret grants,
scoped publication and rejection, not a positive physical MAAS/DPU identity join.
The systemd guest verifies a zero-export baseline without pretending to have
established physical TOR peers.

## Production acceptance still required

- Real MAAS identity/tag discovery, credential privilege and inventory outage /
  leader replacement under the supported deployment topology.
- Actual workload publisher adapters, readiness withdrawal and workload boot
  ordering behind persistent source policy.
- Both TORs, real DPU card/function mappings and surviving hardware-offloaded
  paths, with management/replication isolation and operator rollback protection.
- Idle/load CPU and repeated packet-outage measurements for each supported BFD
  setting; avoid claiming nominal detection time as measured failover time.
- 60-rack Juju controller/event capacity and upgrade/recovery tests.
- ARM acceptance beyond the identity observer: its ARM build in an emulated builder, ELF
  checks and actual hooks on six physical DPUs pass. BIRD and MAAS adapter ARM
  workloads have not been exercised on physical ARM targets.

## Evidence storage

Site-specific inventories, run receipts, restoration plans and operator handoffs
belong in a private deployment workspace. They are not portable product
configuration or proof of the current revision. The tracked examples use
operator-supplied placeholders; rack provisioning tests use synthetic hardware
identifiers and documentation addresses under `tests/fixtures/rack_local`.

A previously passing receipt is not a substitute for checking a new deployment.
