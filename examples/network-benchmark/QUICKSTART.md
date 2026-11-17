# Portable network benchmark toolkit

Extract the archive and use the system Python. No pip, uv, virtual environment,
Python library installation or network download is needed by these scripts.

Required for state capture:

- Controller: Linux, Python 3.10+ and the OpenSSH client (`ssh`).
- Each host/Arm: Linux, SSH access and Python 3.10+ available as `python3`.
- Existing SSH credentials and trusted host keys. `~/.ssh/config` aliases can
  provide nonstandard ports, identity files or jump hosts.

The tarball contains architecture-independent scripts, not Python, system
executables or OS libraries. Capture runs available tools such as `ip`,
`ethtool`, `lspci`, `numactl`, `devlink` and OVS utilities. Missing tools and
permission failures are recorded; nothing is installed. An account permitted
to read OVS/devlink/IRQ data produces a more complete capture. The toolkit
does not invoke sudo or change privileges.

For later traffic tests, both endpoints also need preinstalled iperf3,
numactl, iproute2 (`ip`/`ss`) and iputils `ping`, with the capabilities described
in `docs/network-benchmark.md`. These binaries are not bundled: host and Arm
architectures and installed OS versions can differ. The optional bundled HTML
renderer uses system Node.js 22+, with no npm packages. Capture and JSON reports
do not need Node.js.

## 1. Extract

```sh
tar -xzf network-benchmark.tar.gz
cd network-benchmark
sha256sum -c SHA256SUMS
cp examples/network-benchmark/inventory.json inventory.json
```

All following commands run from this extracted directory, on the controller.
Keep this directory: the controller needs its adjacent agent file when using SSH.

## 2. Fill inventory.json

The sample lists two hosts and two DPU Arms per host. Add or remove entries to
match the machines to capture. Every physical host and DPU Arm needs its own
entry; the host snapshot cannot replace the Arm snapshot.

Fill these fields for each entry:

| Field | Value |
| --- | --- |
| Entry name | Unique label, e.g. `host-a` or `arm-a1`; used in output filenames |
| `role` | `host` or `arm` |
| `ssh` | Reachable SSH target, e.g. `ubuntu@192.0.2.10` or an SSH config alias |
| `identity_kind` | `dmi` (default), or `machine-id` where DMI identity is unavailable |
| `uuid` | Exact value from the selected identity file on that machine |
| `interfaces` | Optional list, e.g. `["p0", "p1", "bond0"]`; omit to discover all interfaces |

Get identity values with these read-only commands, replacing SSH targets:

```sh
# Host: use this output as uuid; identity_kind defaults to dmi.
ssh ubuntu@HOST 'cat /sys/class/dmi/id/product_uuid'
# DPU Arm: use this output as uuid with identity_kind: machine-id.
ssh ubuntu@DPU_ARM 'cat /etc/machine-id'
```

If DMI is not readable by the chosen account, an administrator can read it for
you, or use `identity_kind: machine-id` and `/etc/machine-id` on the host too.
Do not copy the host identity into its Arm entries. Each identity must be unique.
SSH connects noninteractively during capture, so first resolve password/key and
host-key prompts using the normal trusted SSH setup. No firmware, VLAN, MTU,
PCI or NUMA values need filling for inventory-only capture.

## 3. First toolkit command: validate offline

```sh
python3 network_benchmark.py validate inventory.json
```

This checks the inventory format only. It does not contact machines, check
credentials or prove the placeholder values match hardware. Replace every
placeholder before capture. Keep the inventory private.

## 4. Capture current state, without benchmark traffic

```sh
python3 network_benchmark.py capture inventory.json \
  --execute --output evidence/current-state
```

The output directory must not exist. The `--execute` flag here permits SSH
read-only collection; the `capture` subcommand never starts traffic tests or
configures networking. Results contain one `state-NAME.json` per node plus
inventory and script-provenance records. Evidence directories use owner-only
permissions. Read `commands`/`files` in the JSON for unavailable observations;
a successful capture does not imply every optional tool was present.

If capture fails partway, partial evidence is retained. Fix the reported access
or identity issue and retry into a fresh output directory. Do not overwrite a
prior capture.

## Later benchmarking

Use `examples/network-benchmark/manifest.json` and `docs/network-benchmark.md` only after selecting
the supported interface layout and separately configuring the lab. The toolkit
never switches PF/VF attachment mode. Run `python3 network_benchmark.py --help`
for the command list.

If a `renderer/` directory is included, render a benchmark report offline:

```sh
node renderer/bin/report.mjs evidence/comparison/report.json evidence/comparison/report.html
```

`SHA256SUMS` verifies packaged files before editing. It is an integrity manifest,
not a signature from an external trust authority. `BUNDLE.json` records source
hashes and the included files. No credentials, machine evidence, Python packages
or OS binaries are included in the archive.
