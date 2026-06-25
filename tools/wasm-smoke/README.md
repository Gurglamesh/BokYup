# Cross-device WASM smoke test

Proves the headline requirement end-to-end: **a book exported on a PC imports on the
phone, and a book exported on the phone imports on the PC** — across two real Python
runtimes (native CPython ↔ Pyodide/WASM), with all encrypted fields, verifikationer,
moms and receipt photos round-tripping.

It is **not** part of `pytest` (it needs Node + the ~2 MB Pyodide runtime + the crypto
wasm wheels, which are not committed). Run it manually when touching crypto, the
`.buyn` bundle, or the phone boundary.

## Why it matters

The phone runs the *same* Python backend as the PC, compiled to WebAssembly. The only
thing that makes a PC book openable on the phone is that Argon2id derives the **same**
key on both — which holds because `crypto.py` pins `parallelism=1`
(see `tests/test_crypto_vectors.py`). This harness is the integration proof of that.

## Setup (one time)

```bash
cd tools/wasm-smoke
npm init -y && npm i pyodide          # the WASM Python runtime
# Put the crypto wasm wheels where loadPackage finds them (next to the pyodide dist):
#   cryptography, argon2-cffi, argon2-cffi-bindings, cffi, pycparser, six
# They ship inside the Pyodide release bundle (pyodide-<ver>.tar.bz2); copy the
# matching cp3xx wasm32 wheels into node_modules/pyodide/.
```

## Run

```bash
./run.sh                 # uses a fresh temp workdir
./run.sh /path/to/workdir
```

`run.sh` builds `backend_src.zip`, exports a book natively (`pc.buyn`), imports +
verifies it under WASM and exports a phone book (`phone.buyn`), then re-imports that
on native CPython. Any mismatch exits non-zero.
