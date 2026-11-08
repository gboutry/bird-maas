#!/usr/bin/env bash
set -euo pipefail
lab_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$lab_dir/../.."
exec taskset -c 0,1 uv run --locked python "$lab_dir/scripts/cleanup.py" "$@"
