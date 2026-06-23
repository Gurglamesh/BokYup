"""
Tests for Layer 2: DatabaseManager.

Each test gets an isolated temp directory so nothing touches ~/.buyn.
"""

from __future__ import annotations

import os
import pytest
from pathlib import Path

from backend.core.crypto import BadPassphrase, CryptoError, add_recovery_key, unlock, KeyEnvelope
from backend.db.manager import BookRecord, BookSession, DatabaseManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mgr(tmp_path: Path) -> DatabaseManager:
    """A fresh DatabaseManager backed by tmp_path."""
    return DatabaseManager(app_dir=tmp_path / "app")


@pytest.fixture()
def book_and_session(mgr: DatabaseManager, tmp_path: Path):
    """A ready-made open book."""
    db_path = tmp_path / "test.db"
    record, session = mgr.create_book("Test Book", str(db_path), "correct-horse")
    return mgr, record, session


# ---------------------------------------------------------------------------
# create_book
# ---------------------------------------------------------------------------

class TestCreateBook:
    def test_creates_db_and_key_files(self, mgr: DatabaseManager, tmp_path: Path):
        db_path = tmp_path / "mybook.db"
        record, session = mgr.create_book("My Book", str(db_path), "passphrase")
        assert db_path.exists(), ".db file must exist"
        assert Path(str(db_path) + ".key").exists(), ".key file must exist"

    def test_session_is_unlocked(self, mgr: DatabaseManager, tmp_path: Path):
        _, session = mgr.create_book("B", str(tmp_path / "b.db"), "p")
        assert not session.is_locked

    def test_book_appears_in_registry(self, mgr: DatabaseManager, tmp_path: Path):
        _, _ = mgr.create_book("B", str(tmp_path / "b.db"), "p")
        books = mgr.list_books()
        assert len(books) == 1
        assert books[0].display_name == "B"

    def test_duplicate_path_raises(self, mgr: DatabaseManager, tmp_path: Path):
        db_path = str(tmp_path / "dup.db")
        mgr.create_book("A", db_path, "p")
        with pytest.raises(FileExistsError):
            mgr.create_book("B", db_path, "p")

    def test_registry_persists_across_manager_restart(
        self, tmp_path: Path
    ):
        app_dir = tmp_path / "app"
        mgr1 = DatabaseManager(app_dir=app_dir)
        mgr1.create_book("Persisted", str(tmp_path / "p.db"), "pw")

        mgr2 = DatabaseManager(app_dir=app_dir)
        books = mgr2.list_books()
        assert len(books) == 1
        assert books[0].display_name == "Persisted"


# ---------------------------------------------------------------------------
# open_book
# ---------------------------------------------------------------------------

class TestOpenBook:
    def test_correct_passphrase_unlocks(self, book_and_session):
        mgr, record, session = book_and_session
        mgr.lock_book(record.id)
        s = mgr.open_book(record.id, "correct-horse")
        assert not s.is_locked

    def test_wrong_passphrase_raises(self, book_and_session):
        mgr, record, session = book_and_session
        mgr.lock_book(record.id)
        with pytest.raises(BadPassphrase):
            mgr.open_book(record.id, "wrong-passphrase")

    def test_already_unlocked_returns_same_session(self, book_and_session):
        mgr, record, session = book_and_session
        s2 = mgr.open_book(record.id, "correct-horse")
        assert s2 is session

    def test_unknown_book_id_raises(self, mgr: DatabaseManager):
        with pytest.raises(KeyError):
            mgr.open_book("nonexistent-id", "p")

    def test_missing_db_file_raises(self, mgr: DatabaseManager, tmp_path: Path):
        db_path = tmp_path / "gone.db"
        record, _ = mgr.create_book("Gone", str(db_path), "p")
        mgr.lock_book(record.id)
        db_path.unlink()
        with pytest.raises(FileNotFoundError):
            mgr.open_book(record.id, "p")

    def test_last_opened_updated(self, book_and_session):
        mgr, record, session = book_and_session
        before = record.last_opened
        mgr.lock_book(record.id)
        mgr.open_book(record.id, "correct-horse")
        # Reload registry to confirm persistence
        books = {b.id: b for b in mgr.list_books()}
        assert books[record.id].last_opened is not None


# ---------------------------------------------------------------------------
# open_book_with_recovery
# ---------------------------------------------------------------------------

class TestRecoveryKey:
    def test_recovery_key_unlocks(self, book_and_session):
        mgr, record, session = book_and_session
        # Add a recovery key while unlocked (we need the passphrase for that)
        key_path = Path(record.key_path)
        envelope = KeyEnvelope.from_json(key_path.read_text())
        new_envelope, recovery_key = add_recovery_key(envelope, "correct-horse")
        key_path.write_text(new_envelope.to_json())

        mgr.lock_book(record.id)
        s = mgr.open_book_with_recovery(record.id, recovery_key)
        assert not s.is_locked

    def test_wrong_recovery_key_raises(self, book_and_session):
        mgr, record, session = book_and_session
        key_path = Path(record.key_path)
        envelope = KeyEnvelope.from_json(key_path.read_text())
        new_envelope, _ = add_recovery_key(envelope, "correct-horse")
        key_path.write_text(new_envelope.to_json())

        mgr.lock_book(record.id)
        with pytest.raises((BadPassphrase, CryptoError)):
            mgr.open_book_with_recovery(record.id, "WRONG-RECOV-KEY")


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

class TestLocking:
    def test_lock_book_wipes_dek(self, book_and_session):
        mgr, record, session = book_and_session
        mgr.lock_book(record.id)
        assert session.is_locked
        assert mgr.get_session(record.id) is None

    def test_lock_all(self, mgr: DatabaseManager, tmp_path: Path):
        _, s1 = mgr.create_book("A", str(tmp_path / "a.db"), "p1")
        _, s2 = mgr.create_book("B", str(tmp_path / "b.db"), "p2")
        mgr.lock_all()
        assert s1.is_locked
        assert s2.is_locked

    def test_locked_session_raises_on_encrypt(self, book_and_session):
        mgr, record, session = book_and_session
        mgr.lock_book(record.id)
        with pytest.raises(CryptoError):
            session.encrypt_text("secret")


# ---------------------------------------------------------------------------
# Crypto helpers via session
# ---------------------------------------------------------------------------

class TestSessionCrypto:
    def test_encrypt_decrypt_text_roundtrip(self, book_and_session):
        _, _, session = book_and_session
        plaintext = "Faktura #1 — 12 500 kr inkl. moms"
        blob = session.encrypt_text(plaintext)
        assert session.decrypt_text(blob) == plaintext

    def test_encrypt_decrypt_blob_roundtrip(self, book_and_session):
        _, _, session = book_and_session
        data = b"\x89PNG\r\n\x1a\n" + os.urandom(128)
        enc = session.encrypt_blob(data)
        assert session.decrypt_blob(enc) == data

    def test_different_books_produce_different_ciphertexts(
        self, mgr: DatabaseManager, tmp_path: Path
    ):
        _, s1 = mgr.create_book("A", str(tmp_path / "a.db"), "pw")
        _, s2 = mgr.create_book("B", str(tmp_path / "b.db"), "pw")
        ct1 = s1.encrypt_text("hello")
        ct2 = s2.encrypt_text("hello")
        assert ct1 != ct2  # different DEKs → different ciphertexts

    def test_session_b_cannot_decrypt_session_a_data(
        self, mgr: DatabaseManager, tmp_path: Path
    ):
        _, s1 = mgr.create_book("A", str(tmp_path / "a.db"), "pw")
        _, s2 = mgr.create_book("B", str(tmp_path / "b.db"), "pw")
        ct = s1.encrypt_text("secret")
        with pytest.raises(Exception):
            s2.decrypt_text(ct)


# ---------------------------------------------------------------------------
# Registry management
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_register_existing(self, mgr: DatabaseManager, tmp_path: Path):
        # Create via a second manager so the first doesn't know about it
        other_dir = tmp_path / "other_app"
        mgr2 = DatabaseManager(app_dir=other_dir)
        db_path = tmp_path / "foreign.db"
        mgr2.create_book("Foreign", str(db_path), "p")

        rec = mgr.register_existing("Foreign Import", str(db_path))
        assert rec.display_name == "Foreign Import"
        assert len(mgr.list_books()) == 1

    def test_register_existing_idempotent(self, mgr: DatabaseManager, tmp_path: Path):
        other_dir = tmp_path / "other_app"
        mgr2 = DatabaseManager(app_dir=other_dir)
        db_path = tmp_path / "foreign.db"
        mgr2.create_book("Foreign", str(db_path), "p")

        r1 = mgr.register_existing("Name1", str(db_path))
        r2 = mgr.register_existing("Name2", str(db_path))
        assert r1.id == r2.id
        assert len(mgr.list_books()) == 1

    def test_register_existing_missing_db_raises(self, mgr: DatabaseManager, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            mgr.register_existing("X", str(tmp_path / "nope.db"))

    def test_remove_from_registry(self, book_and_session):
        mgr, record, _ = book_and_session
        mgr.remove_from_registry(record.id)
        assert len(mgr.list_books()) == 0
        assert mgr.get_session(record.id) is None

    def test_remove_does_not_delete_files(self, book_and_session):
        mgr, record, _ = book_and_session
        db_path = Path(record.db_path)
        mgr.remove_from_registry(record.id)
        assert db_path.exists()

    def test_rename_book(self, book_and_session):
        mgr, record, _ = book_and_session
        mgr.rename_book(record.id, "Renamed Book")
        books = {b.id: b for b in mgr.list_books()}
        assert books[record.id].display_name == "Renamed Book"

    def test_list_books_sorted_by_last_opened(
        self, mgr: DatabaseManager, tmp_path: Path
    ):
        _, _ = mgr.create_book("First", str(tmp_path / "a.db"), "p")
        _, _ = mgr.create_book("Second", str(tmp_path / "b.db"), "p")
        books = mgr.list_books()
        # Most-recently-opened is first
        assert books[0].display_name == "Second"

    def test_sqlite_connection_via_session(self, book_and_session):
        _, _, session = book_and_session
        conn = session.connection()
        result = conn.execute("SELECT sqlite_version()").fetchone()
        assert result is not None
