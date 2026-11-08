# Pinned MAAS provider

The lab uses downstream prerelease `2.10.0-networklab.1`, not the official
`2.10.0` binary. [source.json](source.json) pins the upstream source, patch
SHA256, Go toolchain/platform and expected binary/archive checksums. The patch
lets Terraform handle already-absent fabrics/VLANs without failing and adds
opt-in subnet retention. Default deletion
behavior stays the same as upstream.

On Linux amd64 with Git, Make, uv and Go 1.27.3, run from the repository root:

```sh
make build-network-lab-provider
```

The helper fetches the pinned public upstream source into a fresh temporary
workspace, verifies and applies the patch, runs the targeted upstream unit tests
and calls upstream `make build`. It limits build parallelism, disables CGO and
automatic toolchain downloads, and keeps the pinned Go dependencies. It removes
compilation paths, VCS metadata and build ID from the binary. Private lab inputs
never enter the build.

The resulting bytes must match the manifest before entering the mirror.
The helper refuses different bytes under an existing prerelease version.
It writes the packed filesystem mirror archive, `terraform.rc` and private
`provider-build.json` under `tests/network-lab/runtime/terraform/providers`.

The wrapper uses this configuration when `TF_CLI_CONFIG_FILE` is unset.
An explicit operator setting takes precedence; global Terraform configuration
stays unchanged. Only `canonical/maas` uses the mirror. Other providers use
their normal installation path. The repository tracks exact constraints and
Linux amd64 locks for the consumers.

When intentionally changing the provider/version, regenerate locks through
initialized consumers and keep unrelated provider entries:

```sh
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root/tests/network-lab/terraform/live/lab/25-maas-config"
terragrunt run -- init -upgrade -input=false
terragrunt run -- providers lock \
  -fs-mirror="$repo_root/tests/network-lab/runtime/terraform/providers/mirror" \
  -platform=linux_amd64 registry.terraform.io/canonical/maas
```

Repeat for `35-commission`, `40-networking` and `45-juju-controller` in dependency
order. Do not refresh dependent source caches concurrently. Review the plan
before applying it; updating locks does not authorize an apply.

Build tests, checksums and schema inspection are only part of provider
validation. Check fresh apply and teardown at the matching source revision, including
MAAS fabric garbage collection and management-subnet retention.
Keep the patch until an upstream release supplies the same behavior.
See [compatibility](../../../docs/compatibility.md).
