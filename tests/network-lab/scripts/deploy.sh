#!/usr/bin/env bash
set -euo pipefail
umask 077
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd -- "$script_dir/../../.."
exec taskset -c 0,1 uv run --locked python "$script_dir/deploy.py" "$@"
