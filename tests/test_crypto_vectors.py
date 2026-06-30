"""
Cross-platform crypto contract tests.

The app derives the KEK on every platform it runs on — Windows/macOS/Linux (native
argon2-cffi) AND the phone, where the same Python crypto core runs as WebAssembly
(Pyodide). For a book exported on a PC to unlock on a phone, the Argon2id derivation
must be BIT-IDENTICAL across platforms. Two rules guarantee that:

  1. parallelism == 1  (Argon2 with >1 lane needs pthreads, which the WASM build
     lacks — it raises "Threading failure" — and multi-lane output can differ by
     threading anyway). A single lane is deterministic everywhere.
  2. The derivation for a fixed (passphrase, salt, t, m, p) is frozen to a known
     vector, so no future change can silently break PC<->phone portability.

The frozen vector below was verified equal on native CPython and on Pyodide 314
(cryptography 47.0.0 + argon2-cffi-bindings 25.1.0, cp314 wasm32 wheels).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from core import crypto as c  # noqa: E402


# Verified identical on native CPython and Pyodide/WASM.
_FIXED_PASSPHRASE = "pc-made-passphrase"
_FIXED_SALT = b"0123456789abcdef"
_FIXED_PARAMS = dict(time_cost=3, memory_cost=65536, parallelism=1)
_EXPECTED_KEK_HEX = "41bfff33eef976f35d00cb8b82fbe56a206d987226b89074f8d5e9a05188a34d"


def test_default_parallelism_is_one():
    # The cross-platform (phone/WASM) contract: new books must be created with a
    # single Argon2 lane, or they cannot be opened on the phone.
    assert c.ARGON2_PARALLELISM == 1


def test_new_books_use_single_lane():
    # create_envelope() with defaults bakes parallelism=1 into every slot.
    env, _ = c.create_envelope("whatever")
    assert all(slot.parallelism == 1 for slot in env.slots)


def test_kek_derivation_vector_is_frozen():
    kek = c._derive_kek(_FIXED_PASSPHRASE, _FIXED_SALT, **_FIXED_PARAMS)
    assert kek.hex() == _EXPECTED_KEK_HEX, (
        "Argon2id KEK derivation changed — this BREAKS cross-platform unlock "
        "(a PC-exported book would no longer open on the phone)."
    )
