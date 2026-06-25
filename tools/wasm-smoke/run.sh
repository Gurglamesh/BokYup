#!/usr/bin/env bash
# Cross-device .buyn round-trip, both directions, native CPython <-> Pyodide/WASM.
# See README.md for setup (needs `npm i pyodide` here + the crypto wasm wheels).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WD="${1:-$(mktemp -d)}"

python "$ROOT/tools/build_phone_assets.py" --out "$WD" >/dev/null
python "$HERE/xdev.py" export "$WD"
node "$HERE/xdev_wasm.mjs" "$WD"
python "$HERE/xdev.py" verify-import "$WD"
echo "CROSS-DEVICE OK (both directions): $WD"
