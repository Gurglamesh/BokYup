"""
crypto.py — Cryptographic core for the bokföring application.

Design (envelope encryption):

    password --Argon2id(salt)--> KEK --wraps--> DEK --encrypts--> data (DB fields + photos)

- The DEK (Data Encryption Key) is generated ONCE per database and never changes.
  It is what actually encrypts data. It is stored only in wrapped (encrypted) form.
- The KEK (Key Encryption Key) is derived from the user's passphrase via Argon2id.
  Its only job is to wrap/unwrap the DEK.
- Changing the passphrase re-derives a new KEK and re-wraps the SAME DEK.
  No bulk re-encryption of data is required (fast + crash-safe).
- An optional recovery key provides a SECOND independent wrapping of the DEK,
  so a forgotten passphrase does not mean permanent loss of a 7-year legal archive.

All symmetric encryption uses AES-256-GCM (authenticated: detects tampering).

This module is pure-Python (argon2-cffi + cryptography), so it installs via pip
on Windows, macOS, Linux — and later inside Android/iOS webview wrappers — with
no native SQLCipher build step.
"""

from __future__ import annotations

import os
import json
import base64
from dataclasses import dataclass

from argon2.low_level import hash_secret_raw, Type
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ---------------------------------------------------------------------------
# Tunable parameters. Stored alongside each wrapped DEK so future parameter
# changes remain backward-compatible (a database remembers how it was made).
# ---------------------------------------------------------------------------

KEY_LEN = 32          # 256-bit keys
SALT_LEN = 16
NONCE_LEN = 12        # AES-GCM standard nonce size

# Argon2id work factors. Defaults aim for ~0.3–1s on a typical laptop.
# time_cost/memory_cost can be raised over time; old DBs keep their own values
# (each wrapped DEK stores the params it was made with).
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 64 * 1024   # 64 MiB, in KiB
# Parallelism MUST stay 1. The KEK is derived on every platform the app runs on,
# including the phone, where the Argon2 stack runs as WebAssembly (Pyodide) with
# NO pthreads — any parallelism > 1 raises "Threading failure" there. With a single
# lane the derivation is also fully deterministic across platforms, so a book made
# (and exported) on a PC unlocks bit-for-bit on the phone. This is what makes the
# "encrypt on PC, import on phone" requirement possible. Strengthen via time_cost/
# memory_cost, never parallelism. (Verified: PC and Pyodide produce identical KEKs.)
ARGON2_PARALLELISM = 1

CRYPTO_FORMAT_VERSION = 1


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class CryptoError(Exception):
    """Base class for all crypto errors."""


class BadPassphrase(CryptoError):
    """Raised when a passphrase (or recovery key) fails to unwrap the DEK."""


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _derive_kek(passphrase: str, salt: bytes,
                time_cost: int, memory_cost: int, parallelism: int) -> bytes:
    """Derive a Key Encryption Key from a passphrase using Argon2id."""
    return hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=time_cost,
        memory_cost=memory_cost,
        parallelism=parallelism,
        hash_len=KEY_LEN,
        type=Type.ID,
    )


def _aesgcm_encrypt(key: bytes, plaintext: bytes, aad: bytes | None = None) -> bytes:
    """Return nonce || ciphertext (ciphertext includes the GCM tag)."""
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return nonce + ct


def _aesgcm_decrypt(key: bytes, blob: bytes, aad: bytes | None = None) -> bytes:
    nonce, ct = blob[:NONCE_LEN], blob[NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, aad)


# ---------------------------------------------------------------------------
# Key wrapping records
# ---------------------------------------------------------------------------

@dataclass
class WrapSlot:
    """One wrapping of the DEK (e.g. by passphrase, or by recovery key)."""
    kind: str          # "passphrase" | "recovery"
    salt: str          # b64
    time_cost: int
    memory_cost: int
    parallelism: int
    wrapped_dek: str   # b64 of nonce||ciphertext

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "salt": self.salt,
            "time_cost": self.time_cost,
            "memory_cost": self.memory_cost,
            "parallelism": self.parallelism,
            "wrapped_dek": self.wrapped_dek,
        }

    @staticmethod
    def from_dict(d: dict) -> "WrapSlot":
        return WrapSlot(
            kind=d["kind"],
            salt=d["salt"],
            time_cost=d["time_cost"],
            memory_cost=d["memory_cost"],
            parallelism=d["parallelism"],
            wrapped_dek=d["wrapped_dek"],
        )


@dataclass
class KeyEnvelope:
    """
    The complete public (non-secret) key material stored for a database.

    Contains the DEK wrapped under one or more slots. Safe to store on disk and
    to ship inside an export bundle: it reveals nothing without the passphrase
    (or recovery key).
    """
    version: int
    slots: list[WrapSlot]

    def to_json(self) -> str:
        return json.dumps(
            {"version": self.version, "slots": [s.to_dict() for s in self.slots]},
            indent=2,
        )

    @staticmethod
    def from_json(text: str) -> "KeyEnvelope":
        d = json.loads(text)
        return KeyEnvelope(
            version=d["version"],
            slots=[WrapSlot.from_dict(s) for s in d["slots"]],
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_envelope(passphrase: str,
                    time_cost: int = ARGON2_TIME_COST,
                    memory_cost: int = ARGON2_MEMORY_COST,
                    parallelism: int = ARGON2_PARALLELISM) -> tuple[KeyEnvelope, bytes]:
    """
    Create a brand-new database key envelope.

    Returns (envelope, dek). The DEK is returned so the caller can immediately
    begin encrypting; it must be kept in memory only while the DB is unlocked.
    """
    dek = os.urandom(KEY_LEN)
    salt = os.urandom(SALT_LEN)
    kek = _derive_kek(passphrase, salt, time_cost, memory_cost, parallelism)
    wrapped = _aesgcm_encrypt(kek, dek, aad=b"dek-wrap-v1")

    slot = WrapSlot(
        kind="passphrase",
        salt=_b64e(salt),
        time_cost=time_cost,
        memory_cost=memory_cost,
        parallelism=parallelism,
        wrapped_dek=_b64e(wrapped),
    )
    return KeyEnvelope(version=CRYPTO_FORMAT_VERSION, slots=[slot]), dek


def _unwrap_with_slot(slot: WrapSlot, secret: str) -> bytes:
    kek = _derive_kek(
        secret,
        _b64d(slot.salt),
        slot.time_cost,
        slot.memory_cost,
        slot.parallelism,
    )
    try:
        return _aesgcm_decrypt(kek, _b64d(slot.wrapped_dek), aad=b"dek-wrap-v1")
    except Exception as exc:  # invalid tag => wrong secret or tampering
        raise BadPassphrase("Wrong passphrase or corrupted key material") from exc


def unlock(envelope: KeyEnvelope, passphrase: str) -> bytes:
    """Recover the DEK from the envelope using the passphrase slot."""
    for slot in envelope.slots:
        if slot.kind == "passphrase":
            return _unwrap_with_slot(slot, passphrase)
    raise CryptoError("No passphrase slot present in envelope")


def unlock_with_recovery(envelope: KeyEnvelope, recovery_key: str) -> bytes:
    """Recover the DEK using the recovery-key slot (if one exists)."""
    for slot in envelope.slots:
        if slot.kind == "recovery":
            return _unwrap_with_slot(slot, recovery_key)
    raise CryptoError("No recovery slot present in envelope")


def change_passphrase(envelope: KeyEnvelope, old_passphrase: str,
                      new_passphrase: str,
                      time_cost: int = ARGON2_TIME_COST,
                      memory_cost: int = ARGON2_MEMORY_COST,
                      parallelism: int = ARGON2_PARALLELISM) -> KeyEnvelope:
    """
    Re-wrap the SAME DEK under a new passphrase. No data re-encryption.

    Returns a new KeyEnvelope; the recovery slot (if any) is preserved untouched.
    """
    dek = unlock(envelope, old_passphrase)

    salt = os.urandom(SALT_LEN)
    kek = _derive_kek(new_passphrase, salt, time_cost, memory_cost, parallelism)
    wrapped = _aesgcm_encrypt(kek, dek, aad=b"dek-wrap-v1")
    new_pass_slot = WrapSlot(
        kind="passphrase",
        salt=_b64e(salt),
        time_cost=time_cost,
        memory_cost=memory_cost,
        parallelism=parallelism,
        wrapped_dek=_b64e(wrapped),
    )

    other_slots = [s for s in envelope.slots if s.kind != "passphrase"]
    return KeyEnvelope(version=envelope.version, slots=[new_pass_slot] + other_slots)


def add_recovery_key(envelope: KeyEnvelope, passphrase: str,
                     recovery_key: str | None = None) -> tuple[KeyEnvelope, str]:
    """
    Add (or replace) a recovery-key slot wrapping the same DEK.

    If recovery_key is None, a strong random one is generated and returned.
    Store the returned recovery key OFFLINE (printed / password manager).
    """
    dek = unlock(envelope, passphrase)

    if recovery_key is None:
        # 32 random bytes -> URL-safe text, grouped for readability.
        raw = os.urandom(20)
        text = base64.b32encode(raw).decode("ascii").rstrip("=")
        recovery_key = "-".join(text[i:i + 5] for i in range(0, len(text), 5))

    salt = os.urandom(SALT_LEN)
    kek = _derive_kek(recovery_key, salt, ARGON2_TIME_COST,
                      ARGON2_MEMORY_COST, ARGON2_PARALLELISM)
    wrapped = _aesgcm_encrypt(kek, dek, aad=b"dek-wrap-v1")
    rec_slot = WrapSlot(
        kind="recovery",
        salt=_b64e(salt),
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        wrapped_dek=_b64e(wrapped),
    )

    non_recovery = [s for s in envelope.slots if s.kind != "recovery"]
    return KeyEnvelope(version=envelope.version, slots=non_recovery + [rec_slot]), recovery_key


# ---------------------------------------------------------------------------
# Data encryption using the DEK (for DB fields and photo blobs)
# ---------------------------------------------------------------------------

def encrypt_blob(dek: bytes, plaintext: bytes, aad: bytes | None = None) -> bytes:
    """Encrypt arbitrary bytes (a receipt photo, a serialized field) with the DEK."""
    return _aesgcm_encrypt(dek, plaintext, aad)


def decrypt_blob(dek: bytes, blob: bytes, aad: bytes | None = None) -> bytes:
    return _aesgcm_decrypt(dek, blob, aad)


def encrypt_text(dek: bytes, text: str, aad: bytes | None = None) -> str:
    """Encrypt a string and return b64 (suitable for a TEXT column)."""
    return _b64e(_aesgcm_encrypt(dek, text.encode("utf-8"), aad))


def decrypt_text(dek: bytes, b64blob: str, aad: bytes | None = None) -> str:
    return _aesgcm_decrypt(dek, _b64d(b64blob), aad).decode("utf-8")
