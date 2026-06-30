"""
bundle.py — Layer 5: Export / import of a single book as a `.buyn` bundle.

A `.buyn` file is just a zip containing one legal entity's complete, portable books.
The same bundle doubles as the **encrypted backup** mechanism (CLAUDE.md > Export/import).

Bundle layout:

    manifest.json          - format/app/schema versions, timestamp, per-file checksums
    data/book.db           - the SQLite database (field-encrypted; structure is plaintext)
    data/book.key          - the KeyEnvelope (wrapped DEK) — Option A: travels with the data
    data/photos/...         - encrypted receipt photos, if any exist beside the db

Because we use application-level *field* encryption (not SQLCipher), the SQLite file
itself is a normal database: its schema version is readable without the passphrase,
but the sensitive columns/blobs are ciphertext. Option A means the recipient unlocks
the imported book with the SAME passphrase (the wrapped DEK is in the bundle).

Import is **full restore / replace**, never merge — merging would break the unbroken
verifikationsnummer sequence (explicitly out of scope). Before overwriting an existing
database, the current state is auto-backed-up to its own `.buyn` first.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from backend import __version__ as APP_VERSION
from backend.models import schema as S

BUNDLE_FORMAT_VERSION = 1
BUNDLE_SUFFIX = ".buyn"

_DB_ARC = "data/book.db"
_KEY_ARC = "data/book.key"
_PHOTOS_PREFIX = "data/photos/"
_MANIFEST_ARC = "manifest.json"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class BundleError(Exception):
    """Base class for export/import errors."""


class ChecksumMismatch(BundleError):
    """A file in the bundle does not match its manifest checksum (corruption/tamper)."""


class SchemaVersionError(BundleError):
    """The bundle's schema version is not compatible with this app version."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _photos_dir(db_path: Path) -> Path:
    """Convention: receipt photos live in a sibling `<db>.photos/` directory."""
    return Path(str(db_path) + ".photos")


def _consistent_db_snapshot(src_db: Path) -> bytes:
    """
    Return a consistent byte snapshot of the SQLite db via the backup API.

    Backing up into a single temp file (rather than reading the live file) folds in
    any committed WAL and is safe even while another connection has the db open.
    """
    import tempfile

    src = sqlite3.connect(str(src_db))
    try:
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td) / "snapshot.db"
            dst = sqlite3.connect(str(snap))
            try:
                src.backup(dst)
            finally:
                dst.close()
            return snap.read_bytes()
    finally:
        src.close()


def _read_schema_version(db_path: Path) -> int:
    """Read PRAGMA user_version directly (db structure is plaintext under field encryption)."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _check_schema_compatible(version: int) -> None:
    """
    Gate a bundle's schema version on import. A NEWER bundle is refused (this app
    can't know future tables). An OLDER bundle is fine: it is migrated forward after
    restore (see `import_` → `schema.migrate`). All migrations are pure DDL, so they
    run on the restored file without needing the passphrase/DEK.
    """
    if version > S.SCHEMA_VERSION:
        raise SchemaVersionError(
            f"Bundle schema v{version} is newer than this app (v{S.SCHEMA_VERSION}); "
            "upgrade the application to import it."
        )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export(db_path: str | Path, out_path: str | Path, *, display_name: str = "") -> Path:
    """
    Export the book at `db_path` (its `.db` + `.db.key` [+ `.photos/`]) to a `.buyn`
    bundle at `out_path`. Does not require the book to be unlocked.
    """
    db_path = Path(db_path)
    key_path = Path(str(db_path) + ".key")
    if not db_path.exists():
        raise BundleError(f"Database not found: {db_path}")
    if not key_path.exists():
        raise BundleError(f"Key envelope not found: {key_path}")

    out_path = Path(out_path)
    if out_path.suffix != BUNDLE_SUFFIX:
        out_path = out_path.with_suffix(BUNDLE_SUFFIX)

    db_bytes = _consistent_db_snapshot(db_path)
    key_bytes = key_path.read_bytes()
    schema_version = _read_schema_version(db_path)

    # Collect file entries (arcname -> bytes).
    entries: dict[str, bytes] = {_DB_ARC: db_bytes, _KEY_ARC: key_bytes}
    photos = _photos_dir(db_path)
    if photos.is_dir():
        for f in sorted(photos.rglob("*")):
            if f.is_file():
                arc = _PHOTOS_PREFIX + f.relative_to(photos).as_posix()
                entries[arc] = f.read_bytes()

    manifest = {
        "format_version": BUNDLE_FORMAT_VERSION,
        "app_version": APP_VERSION,
        "schema_version": schema_version,
        "display_name": display_name,
        "exported_at": _now(),
        "files": {
            arc: {"sha256": _sha256(b), "size": len(b)} for arc, b in entries.items()
        },
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(_MANIFEST_ARC, json.dumps(manifest, indent=2, ensure_ascii=False))
        for arc, b in entries.items():
            z.writestr(arc, b)
    return out_path


# ---------------------------------------------------------------------------
# Inspect / verify
# ---------------------------------------------------------------------------

def read_manifest(bundle_path: str | Path) -> dict:
    """Read and return the bundle manifest without extracting anything."""
    with zipfile.ZipFile(bundle_path) as z:
        return json.loads(z.read(_MANIFEST_ARC))


def verify_bundle(bundle_path: str | Path) -> dict:
    """
    Verify every file's SHA-256 against the manifest. Returns the manifest on
    success; raises ChecksumMismatch on the first discrepancy.
    """
    with zipfile.ZipFile(bundle_path) as z:
        manifest = json.loads(z.read(_MANIFEST_ARC))
        for arc, info in manifest["files"].items():
            try:
                data = z.read(arc)
            except KeyError as exc:
                raise ChecksumMismatch(f"Missing file in bundle: {arc}") from exc
            if _sha256(data) != info["sha256"]:
                raise ChecksumMismatch(f"Checksum mismatch for {arc}")
    return manifest


# ---------------------------------------------------------------------------
# Import (full restore / replace)
# ---------------------------------------------------------------------------

def import_(bundle_path: str | Path, dest_db_path: str | Path, *,
            overwrite: bool = False, backup: bool = True) -> dict:
    """
    Restore a `.buyn` bundle to `dest_db_path` (writing `.db` + `.db.key`
    [+ `.photos/`]). Verifies checksums and schema compatibility first.

    If `dest_db_path` already exists, `overwrite=True` is required and (unless
    `backup=False`) the current state is auto-exported to a timestamped `.buyn`
    backup beside it before being replaced.

    Returns a dict: {manifest, db_path, key_path, backup_path}.
    """
    bundle_path = Path(bundle_path)
    dest_db = Path(dest_db_path)
    dest_key = Path(str(dest_db) + ".key")

    manifest = verify_bundle(bundle_path)
    _check_schema_compatible(manifest["schema_version"])

    backup_path = None
    if dest_db.exists():
        if not overwrite:
            raise BundleError(
                f"Destination already exists: {dest_db}. Pass overwrite=True to replace it."
            )
        if backup and dest_key.exists():
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = export(
                dest_db, dest_db.with_name(f"{dest_db.stem}.backup-{ts}{BUNDLE_SUFFIX}"),
                display_name="auto-backup before import",
            )

    dest_db.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle_path) as z:
        dest_db.write_bytes(z.read(_DB_ARC))
        dest_key.write_bytes(z.read(_KEY_ARC))

        # Restore photos (replace the directory contents).
        photos = _photos_dir(dest_db)
        for arc in manifest["files"]:
            if arc.startswith(_PHOTOS_PREFIX):
                rel = arc[len(_PHOTOS_PREFIX):]
                target = photos / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(arc))

    # Bring an older bundle up to the current schema. Migrations are pure DDL (they
    # never touch encrypted columns), so this runs on the restored file without the
    # passphrase; the book then opens/unlocks normally with its original passphrase.
    migrated_to = manifest["schema_version"]
    if migrated_to < S.SCHEMA_VERSION:
        conn = sqlite3.connect(str(dest_db))
        try:
            migrated_to = S.migrate(conn)
        finally:
            conn.close()

    return {
        "manifest": manifest,
        "db_path": str(dest_db),
        "key_path": str(dest_key),
        "backup_path": str(backup_path) if backup_path else None,
        "schema_version": migrated_to,
    }
