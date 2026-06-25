"""
manager.py — Layer 2: Registry of known databases ("tabs") and session management.

Each "book" is one legal entity's encrypted database. The registry is a JSON
file that stores only non-secret pointers: display name, file path, last opened.
Passphrases and DEKs are NEVER written to disk — DEKs live in memory only while
the session is unlocked.

File layout per book (two files, same stem):
    <name>.db   — SQLite database (field-encrypted via the DEK)
    <name>.key  — KeyEnvelope JSON (the wrapped DEK, safe to store)

The registry is stored at <app_dir>/registry.json.
<app_dir> defaults to ~/.buyn but can be overridden via BUYN_DATA_DIR env var.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.core.crypto import (
    BadPassphrase,  # noqa: F401 — re-exported for callers
    CryptoError,    # noqa: F401
    KeyEnvelope,
    add_recovery_key,
    change_passphrase,
    create_envelope,
    decrypt_blob,
    decrypt_text,
    encrypt_blob,
    encrypt_text,
    unlock,
    unlock_with_recovery,
)

REGISTRY_VERSION = 1

APP_DIR = Path(os.environ.get("BUYN_DATA_DIR", Path.home() / ".buyn"))


# ---------------------------------------------------------------------------
# Registry record (one entry per known book)
# ---------------------------------------------------------------------------

@dataclass
class BookRecord:
    """Non-secret descriptor for one known database, stored in the registry."""
    id: str            # UUID, stable identifier
    display_name: str  # human-readable name (e.g. "Enskild firma 2024")
    db_path: str       # absolute path to the .db file
    last_opened: Optional[str]  # UTC ISO-8601 timestamp or None

    @property
    def key_path(self) -> str:
        return self.db_path + ".key"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "db_path": self.db_path,
            "last_opened": self.last_opened,
        }

    @staticmethod
    def from_dict(d: dict) -> "BookRecord":
        return BookRecord(
            id=d["id"],
            display_name=d["display_name"],
            db_path=d["db_path"],
            last_opened=d.get("last_opened"),
        )


# ---------------------------------------------------------------------------
# Session (one unlocked book in memory)
# ---------------------------------------------------------------------------

class BookSession:
    """
    An unlocked database session. Holds the DEK in memory.

    Call lock() to wipe the DEK (implements auto-lock). After locking, the
    session must be re-opened via DatabaseManager.open_book().
    """

    def __init__(self, record: BookRecord, dek: bytes) -> None:
        self.record = record
        self._dek: Optional[bytes] = dek
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Lock state
    # ------------------------------------------------------------------

    @property
    def is_locked(self) -> bool:
        return self._dek is None

    def lock(self) -> None:
        """Wipe DEK from memory and close the DB connection."""
        if self._dek is not None:
            # Overwrite the key material before releasing the reference.
            self._dek = b"\x00" * len(self._dek)
            self._dek = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _require_unlocked(self) -> bytes:
        if self._dek is None:
            raise CryptoError("Session is locked — re-open with passphrase")
        return self._dek

    # ------------------------------------------------------------------
    # SQLite connection
    # ------------------------------------------------------------------

    def connection(self) -> sqlite3.Connection:
        """Return a shared SQLite connection, creating it on first call."""
        self._require_unlocked()
        if self._conn is None:
            self._conn = sqlite3.connect(self.record.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    # ------------------------------------------------------------------
    # Crypto helpers (thin wrappers so callers never touch the raw DEK)
    # ------------------------------------------------------------------

    def encrypt_text(self, text: str, aad: bytes | None = None) -> str:
        return encrypt_text(self._require_unlocked(), text, aad)

    def decrypt_text(self, b64blob: str, aad: bytes | None = None) -> str:
        return decrypt_text(self._require_unlocked(), b64blob, aad)

    def encrypt_blob(self, data: bytes, aad: bytes | None = None) -> bytes:
        return encrypt_blob(self._require_unlocked(), data, aad)

    def decrypt_blob(self, blob: bytes, aad: bytes | None = None) -> bytes:
        return decrypt_blob(self._require_unlocked(), blob, aad)


# ---------------------------------------------------------------------------
# Database manager (one instance per app lifetime)
# ---------------------------------------------------------------------------

class DatabaseManager:
    """
    Manages the registry of known books and the map of open sessions.

    One DatabaseManager lives for the lifetime of the application.
    Sessions (open books) are keyed by the book's UUID string.
    """

    def __init__(self, app_dir: Path = APP_DIR) -> None:
        self.app_dir = app_dir
        self.app_dir.mkdir(parents=True, exist_ok=True)
        self._registry_path = app_dir / "registry.json"
        self._registry: dict[str, BookRecord] = {}
        self._sessions: dict[str, BookSession] = {}
        self._load_registry()

    # ------------------------------------------------------------------
    # Registry persistence (atomic write)
    # ------------------------------------------------------------------

    def _load_registry(self) -> None:
        if not self._registry_path.exists():
            return
        data = json.loads(self._registry_path.read_text(encoding="utf-8"))
        for entry in data.get("books", []):
            rec = BookRecord.from_dict(entry)
            self._registry[rec.id] = rec

    def _save_registry(self) -> None:
        payload = {
            "version": REGISTRY_VERSION,
            "books": [r.to_dict() for r in self._registry.values()],
        }
        tmp = self._registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._registry_path)  # atomic on POSIX; near-atomic on Windows

    # ------------------------------------------------------------------
    # Querying the registry
    # ------------------------------------------------------------------

    def list_books(self) -> list[BookRecord]:
        """Return all registered books, most-recently-opened first."""
        def sort_key(r: BookRecord) -> str:
            return r.last_opened or ""
        return sorted(self._registry.values(), key=sort_key, reverse=True)

    def get_book(self, book_id: str) -> BookRecord:
        """Return the record for book_id. Raises KeyError if not found."""
        return self._registry[book_id]

    # ------------------------------------------------------------------
    # Creating a new book
    # ------------------------------------------------------------------

    def create_book(
        self,
        display_name: str,
        db_path: str | Path,
        passphrase: str,
    ) -> tuple[BookRecord, BookSession]:
        """
        Create a brand-new encrypted database.

        Generates a fresh DEK, writes the KeyEnvelope to <db_path>.key,
        creates an empty SQLite file at db_path, registers the book, and
        returns an already-unlocked session.

        Raises FileExistsError if db_path already exists.
        """
        db_path = str(Path(db_path).resolve())
        if Path(db_path).exists():
            raise FileExistsError(f"Database already exists: {db_path}")

        envelope, dek = create_envelope(passphrase)
        Path(db_path + ".key").write_text(envelope.to_json(), encoding="utf-8")

        # Touch the SQLite file.
        conn = sqlite3.connect(db_path)
        conn.close()

        record = BookRecord(
            id=str(uuid.uuid4()),
            display_name=display_name,
            db_path=db_path,
            last_opened=_utcnow(),
        )
        self._registry[record.id] = record
        self._save_registry()

        session = BookSession(record, dek)
        self._sessions[record.id] = session
        return record, session

    # ------------------------------------------------------------------
    # Opening / unlocking an existing book
    # ------------------------------------------------------------------

    def open_book(self, book_id: str, passphrase: str) -> BookSession:
        """
        Unlock and open a registered book with its passphrase.

        If the session is already open (not locked), it is returned directly.

        Raises KeyError if book_id is not in the registry.
        Raises FileNotFoundError if the .db or .key file is missing on disk.
        Raises BadPassphrase if the passphrase is wrong.
        """
        record = self._registry[book_id]

        existing = self._sessions.get(book_id)
        if existing is not None and not existing.is_locked:
            return existing

        _assert_files_exist(record)
        envelope = _load_envelope(record)
        dek = unlock(envelope, passphrase)  # raises BadPassphrase on failure

        record.last_opened = _utcnow()
        self._save_registry()

        session = BookSession(record, dek)
        self._sessions[book_id] = session
        _migrate_schema(session)
        return session

    def open_book_with_recovery(
        self, book_id: str, recovery_key: str
    ) -> BookSession:
        """Unlock using the recovery-key slot instead of the passphrase."""
        record = self._registry[book_id]
        _assert_files_exist(record)
        envelope = _load_envelope(record)
        dek = unlock_with_recovery(envelope, recovery_key)

        record.last_opened = _utcnow()
        self._save_registry()

        session = BookSession(record, dek)
        self._sessions[book_id] = session
        _migrate_schema(session)
        return session

    # ------------------------------------------------------------------
    # Key management (re-wrap the same DEK — no bulk re-encryption)
    # ------------------------------------------------------------------

    def change_passphrase(self, book_id: str, old_passphrase: str,
                          new_passphrase: str) -> None:
        """
        Re-wrap the DEK under a new passphrase. The recovery slot (if any) is kept.
        Verifies the old passphrase (raises BadPassphrase on mismatch). Does not
        touch any data — only the <db>.key envelope is rewritten.
        """
        record = self._registry[book_id]
        _assert_files_exist(record)
        envelope = _load_envelope(record)
        new_envelope = change_passphrase(envelope, old_passphrase, new_passphrase)
        _write_envelope(record, new_envelope)

    def add_recovery_key(self, book_id: str, passphrase: str,
                         recovery_key: Optional[str] = None) -> str:
        """
        Add (or replace) a recovery-key slot wrapping the same DEK and return the
        recovery key. If `recovery_key` is None a strong random one is generated.
        The user must store it OFFLINE — it can unlock the book if the passphrase
        is forgotten (protecting a 7-year legal archive).
        """
        record = self._registry[book_id]
        _assert_files_exist(record)
        envelope = _load_envelope(record)
        new_envelope, key = add_recovery_key(envelope, passphrase, recovery_key)
        _write_envelope(record, new_envelope)
        return key

    def has_recovery_key(self, book_id: str) -> bool:
        """Whether the book's envelope already carries a recovery slot."""
        record = self._registry[book_id]
        _assert_files_exist(record)
        return any(slot.kind == "recovery" for slot in _load_envelope(record).slots)

    # ------------------------------------------------------------------
    # Session access
    # ------------------------------------------------------------------

    def get_session(self, book_id: str) -> Optional[BookSession]:
        """Return the open (unlocked) session, or None if locked/not open."""
        session = self._sessions.get(book_id)
        if session is None or session.is_locked:
            return None
        return session

    # ------------------------------------------------------------------
    # Locking
    # ------------------------------------------------------------------

    def lock_book(self, book_id: str) -> None:
        """Lock one book (wipe its DEK from memory)."""
        session = self._sessions.get(book_id)
        if session is not None:
            session.lock()

    def lock_all(self) -> None:
        """Lock every open session (e.g. on app suspend / minimize)."""
        for session in self._sessions.values():
            session.lock()

    # ------------------------------------------------------------------
    # Registry management (no file deletion — that is the user's choice)
    # ------------------------------------------------------------------

    def register_existing(self, display_name: str, db_path: str | Path) -> BookRecord:
        """
        Add an already-existing database to the registry without unlocking it.

        Useful after an import or when opening a file from another device.
        If the path is already registered, returns the existing record.

        Raises FileNotFoundError if the .db or .key file is missing.
        """
        db_path = str(Path(db_path).resolve())
        key_path = db_path + ".key"
        if not Path(db_path).exists():
            raise FileNotFoundError(f"Database file not found: {db_path}")
        if not Path(key_path).exists():
            raise FileNotFoundError(f"Key envelope not found: {key_path}")

        for rec in self._registry.values():
            if rec.db_path == db_path:
                return rec

        record = BookRecord(
            id=str(uuid.uuid4()),
            display_name=display_name,
            db_path=db_path,
            last_opened=None,
        )
        self._registry[record.id] = record
        self._save_registry()
        return record

    def remove_from_registry(self, book_id: str) -> None:
        """
        Remove a book from the registry. Does NOT delete the database files.
        Locks the session first if it is open.
        """
        self.lock_book(book_id)
        self._sessions.pop(book_id, None)
        self._registry.pop(book_id, None)
        self._save_registry()

    def rename_book(self, book_id: str, new_name: str) -> None:
        """Update the display name of a registered book."""
        self._registry[book_id].display_name = new_name
        self._save_registry()

    # ------------------------------------------------------------------
    # Export / import (Layer 5 — `.buyn` bundle, doubles as backup)
    # ------------------------------------------------------------------

    def export_book(self, book_id: str, out_path: str | Path) -> Path:
        """Export a registered book to a `.buyn` bundle. Works while locked."""
        from backend.db import bundle  # local import avoids a circular dependency

        record = self._registry[book_id]
        _assert_files_exist(record)
        return bundle.export(record.db_path, out_path, display_name=record.display_name)

    def import_book(self, bundle_path: str | Path, dest_db_path: str | Path, *,
                    display_name: Optional[str] = None, overwrite: bool = False) -> BookRecord:
        """
        Restore a `.buyn` bundle to dest_db_path (full replace) and register it.

        Auto-backs-up any existing database at the destination first. If a book at
        that path is currently open, it is locked before being overwritten.
        """
        from backend.db import bundle  # local import avoids a circular dependency

        dest_db_path = str(Path(dest_db_path).resolve())
        for bid, rec in self._registry.items():
            if rec.db_path == dest_db_path:
                self.lock_book(bid)  # release file handles before overwrite

        result = bundle.import_(bundle_path, dest_db_path, overwrite=overwrite)
        name = display_name or result["manifest"].get("display_name") or Path(dest_db_path).stem
        record = self.register_existing(name, dest_db_path)
        if display_name:
            self.rename_book(record.id, display_name)
        return record


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate_schema(session: "BookSession") -> None:
    """Bring an opened book's schema up to date (no-op if already current)."""
    from backend.models import schema  # local import avoids a circular dependency
    schema.migrate(session.connection())


def _assert_files_exist(record: BookRecord) -> None:
    if not Path(record.db_path).exists():
        raise FileNotFoundError(f"Database file not found: {record.db_path}")
    if not Path(record.key_path).exists():
        raise FileNotFoundError(f"Key envelope not found: {record.key_path}")


def _load_envelope(record: BookRecord) -> KeyEnvelope:
    return KeyEnvelope.from_json(Path(record.key_path).read_text(encoding="utf-8"))


def _write_envelope(record: BookRecord, envelope: KeyEnvelope) -> None:
    """Atomically replace the <db>.key envelope (write temp, then rename)."""
    key_path = Path(record.key_path)
    tmp = key_path.with_suffix(key_path.suffix + ".tmp")
    tmp.write_text(envelope.to_json(), encoding="utf-8")
    tmp.replace(key_path)
