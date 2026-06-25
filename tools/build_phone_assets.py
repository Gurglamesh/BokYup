#!/usr/bin/env python3
"""
build_phone_assets.py — assemble the offline assets the phone (Capacitor) build needs.

The phone runs the SAME Python backend as the PC, in-process under Pyodide (WASM).
For that, the web build must ship three things locally (no CDN — offline + privacy):

  1. backend_src.zip      — the backend/ Python package (this script builds it)
  2. the Pyodide runtime  — pyodide.js, pyodide.asm.wasm, python_stdlib.zip,
                            pyodide-lock.json (from the Pyodide distribution)
  3. the crypto wheels    — cryptography + argon2-cffi(+bindings) + cffi + pycparser
                            + six, the cp3xx wasm32 wheels matching the Pyodide version

Only (1) is produced here, because it is the only piece that lives in this repo.
(2) and (3) are large prebuilt binaries fetched at build time; this script prints
exactly which files to drop into <vendor>. On a normal dev machine:

    pip install pyodide-build   # or: npm i pyodide
    # copy node_modules/pyodide/{pyodide.js,pyodide.asm.*,python_stdlib.zip,
    #   pyodide-lock.json} into <vendor>/, then add the wasm wheels for
    #   cryptography/argon2-cffi/argon2-cffi-bindings/cffi/pycparser/six.

Run:  python tools/build_phone_assets.py [--out backend/api/static/vendor]
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

# Wheels required in <vendor> (filenames vary with the Pyodide release; these are
# the packages, resolved from the distribution's pyodide-lock.json at build time).
REQUIRED_WHEEL_PACKAGES = [
    "cryptography", "argon2-cffi", "argon2-cffi-bindings", "cffi", "pycparser", "six",
]
REQUIRED_RUNTIME_FILES = [
    "pyodide.js", "pyodide.asm.js", "pyodide.asm.wasm",
    "python_stdlib.zip", "pyodide-lock.json",
]


def build_backend_zip(out_dir: Path) -> Path:
    """Zip backend/**/*.py preserving package layout, so Pyodide can import it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "backend_src.zip"
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(BACKEND.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            z.write(path, path.relative_to(ROOT).as_posix())
            count += 1
    print(f"  backend_src.zip: {count} modules -> {target} ({target.stat().st_size} bytes)")
    return target


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "backend/api/static/vendor"),
                    help="vendor directory the phone build serves assets from")
    args = ap.parse_args()
    out = Path(args.out)

    print("Building phone assets:")
    build_backend_zip(out)

    have = {f.name for f in out.glob("*")}
    missing_runtime = [f for f in REQUIRED_RUNTIME_FILES if f not in have]
    missing_wheels = [p for p in REQUIRED_WHEEL_PACKAGES
                      if not any(n.startswith(p.replace("-", "_")) and n.endswith(".whl")
                                 for n in have)]
    if missing_runtime or missing_wheels:
        print("\nStill needed in", out, "(prebuilt Pyodide binaries — fetch at build time):")
        for f in missing_runtime:
            print("   runtime:", f)
        for p in missing_wheels:
            print("   wheel  :", p, "(cp3xx wasm32 wheel matching the Pyodide release)")
    else:
        print("\nAll phone assets present in", out)


if __name__ == "__main__":
    main()
