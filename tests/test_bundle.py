"""
Tests for Layer 5: `.buyn` export/import bundle.

Covers the full round-trip (export -> import -> reopen with same passphrase, data
intact), checksum verification, schema-version refusal, overwrite protection, and
the auto-backup-before-overwrite behaviour.
"""

from __future__ import annotations

import json
import os
import zipfile
import pytest
from pathlib import Path

from backend.db import bundle
from backend.db.manager import DatabaseManager
from backend.db.operations import BookOps
from backend.models import schema as S


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_book(tmp_path: Path, name="Book", fname="book.db", pw="pw"):
    mgr = DatabaseManager(app_dir=tmp_path / "app")
    record, session = mgr.create_book(name, str(tmp_path / fname), pw)
    S.initialize_schema(session.connection())
    return mgr, record, session


def _seed_data(session) -> int:
    """Book one expense; return the verifikation count."""
    ops = BookOps(session)
    cat = ops.create_category("Kontorsmaterial", "expense", 5460)
    ops.record_expense(None, cat, [{"rate_code": "25", "amount_ore": 1250}],
                       "2026-02-01", paid_date="2026-02-01")
    return session.connection().execute("SELECT COUNT(*) FROM verifikation").fetchone()[0]


def _rewrite_zip_entry(path: Path, arcname: str, new_bytes: bytes) -> None:
    """Rebuild a zip with one entry's bytes replaced (for tamper tests)."""
    tmp = str(path) + ".tmp"
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w") as zout:
        for item in zin.infolist():
            data = new_bytes if item.filename == arcname else zin.read(item.filename)
            zout.writestr(item, data)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

class TestExport:
    def test_creates_buyn_file_with_expected_entries(self, tmp_path):
        mgr, record, session = _make_book(tmp_path)
        _seed_data(session)
        out = mgr.export_book(record.id, tmp_path / "export")
        assert out.suffix == ".buyn"
        with zipfile.ZipFile(out) as z:
            names = set(z.namelist())
        assert {"manifest.json", "data/book.db", "data/book.key"} <= names

    def test_manifest_has_versions_and_checksums(self, tmp_path):
        mgr, record, session = _make_book(tmp_path)
        _seed_data(session)
        out = mgr.export_book(record.id, tmp_path / "export.buyn")
        m = bundle.read_manifest(out)
        assert m["schema_version"] == S.SCHEMA_VERSION
        assert m["format_version"] == bundle.BUNDLE_FORMAT_VERSION
        assert "data/book.db" in m["files"]
        assert len(m["files"]["data/book.db"]["sha256"]) == 64

    def test_export_works_while_locked(self, tmp_path):
        mgr, record, session = _make_book(tmp_path)
        _seed_data(session)
        mgr.lock_book(record.id)
        out = mgr.export_book(record.id, tmp_path / "locked.buyn")
        assert out.exists()

    def test_photos_included(self, tmp_path):
        mgr, record, session = _make_book(tmp_path)
        photos = Path(record.db_path + ".photos")
        photos.mkdir()
        (photos / "receipt1.bin").write_bytes(b"encrypted-photo-bytes")
        out = mgr.export_book(record.id, tmp_path / "withphotos.buyn")
        with zipfile.ZipFile(out) as z:
            assert "data/photos/receipt1.bin" in z.namelist()


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

class TestVerify:
    def test_valid_bundle_verifies(self, tmp_path):
        mgr, record, session = _make_book(tmp_path)
        _seed_data(session)
        out = mgr.export_book(record.id, tmp_path / "v.buyn")
        assert bundle.verify_bundle(out)["schema_version"] == S.SCHEMA_VERSION

    def test_tampered_db_detected(self, tmp_path):
        mgr, record, session = _make_book(tmp_path)
        _seed_data(session)
        out = mgr.export_book(record.id, tmp_path / "t.buyn")
        _rewrite_zip_entry(out, "data/book.db", b"corrupted")
        with pytest.raises(bundle.ChecksumMismatch):
            bundle.verify_bundle(out)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_import_to_new_path_preserves_data(self, tmp_path):
        mgr, record, session = _make_book(tmp_path, pw="secret")
        count = _seed_data(session)

        out = mgr.export_book(record.id, tmp_path / "rt.buyn")
        dest = tmp_path / "restored.db"
        rec2 = mgr.import_book(out, dest, display_name="Restored")

        s2 = mgr.open_book(rec2.id, "secret")  # SAME passphrase (Option A)
        got = s2.connection().execute("SELECT COUNT(*) FROM verifikation").fetchone()[0]
        assert got == count

    def test_imported_book_decrypts_sensitive_fields(self, tmp_path):
        mgr, record, session = _make_book(tmp_path, pw="secret")
        ops = BookOps(session)
        ops.create_customer("private", first_name="Anna", personnummer="811218-9876")

        out = mgr.export_book(record.id, tmp_path / "rt2.buyn")
        dest = tmp_path / "restored2.db"
        rec2 = mgr.import_book(out, dest)
        s2 = mgr.open_book(rec2.id, "secret")
        pnr = BookOps(s2).get_customer(1)["personnummer"]
        assert pnr == "811218-9876"

    def test_photos_round_trip(self, tmp_path):
        mgr, record, session = _make_book(tmp_path)
        photos = Path(record.db_path + ".photos")
        photos.mkdir()
        (photos / "r.bin").write_bytes(b"photo")
        out = mgr.export_book(record.id, tmp_path / "p.buyn")

        dest = tmp_path / "restored3.db"
        mgr.import_book(out, dest)
        assert (Path(str(dest) + ".photos") / "r.bin").read_bytes() == b"photo"

    def test_attached_receipt_survives_and_decrypts(self, tmp_path):
        mgr, record, session = _make_book(tmp_path, pw="secret")
        ops = BookOps(session)
        cat = ops.create_category("Kontorsmaterial", "expense", 5460)
        tid = ops.record_expense(None, cat, [{"rate_code": "25", "amount_ore": 1250}],
                                 "2026-02-01")["transaktion_id"]
        data = b"\xff\xd8\xff receipt jpeg bytes \x00\x10"
        ops.attach_receipt(tid, data, "image/jpeg", "paper")

        out = mgr.export_book(record.id, tmp_path / "rc.buyn")
        dest = tmp_path / "restored_rc.db"
        rec2 = mgr.import_book(out, dest)
        s2 = mgr.open_book(rec2.id, "secret")
        ops2 = BookOps(s2)
        rid = ops2.list_receipts(tid)[0]["id"]
        got, mime = ops2.get_receipt(rid)
        assert got == data and mime == "image/jpeg"


# ---------------------------------------------------------------------------
# Safety: overwrite protection, auto-backup, schema version
# ---------------------------------------------------------------------------

class TestSafety:
    def test_overwrite_requires_flag(self, tmp_path):
        mgr, record, session = _make_book(tmp_path)
        _seed_data(session)
        out = mgr.export_book(record.id, tmp_path / "s.buyn")
        # importing onto the SAME existing db without overwrite must refuse
        with pytest.raises(bundle.BundleError):
            bundle.import_(out, record.db_path, overwrite=False)

    def test_auto_backup_before_overwrite(self, tmp_path):
        mgr, record, session = _make_book(tmp_path, pw="secret")
        _seed_data(session)
        out = mgr.export_book(record.id, tmp_path / "s2.buyn")
        mgr.lock_book(record.id)

        result = bundle.import_(out, record.db_path, overwrite=True)
        assert result["backup_path"] is not None
        assert Path(result["backup_path"]).exists()

    def test_newer_schema_refused(self, tmp_path):
        mgr, record, session = _make_book(tmp_path)
        _seed_data(session)
        out = mgr.export_book(record.id, tmp_path / "sv.buyn")
        # bump the manifest's schema_version beyond what the app supports
        m = bundle.read_manifest(out)
        m["schema_version"] = S.SCHEMA_VERSION + 1
        _rewrite_zip_entry(out, "manifest.json", json.dumps(m).encode())
        with pytest.raises(bundle.SchemaVersionError):
            bundle.import_(out, tmp_path / "willnot.db")

    def test_older_schema_migrated_forward_on_import(self, tmp_path):
        import sqlite3

        mgr, record, session = _make_book(tmp_path)
        _seed_data(session)
        # Simulate a book exported by an older app version: drop a table that a later
        # migration introduces and roll PRAGMA user_version back to before it.
        conn = session.connection()
        conn.execute("DROP TABLE IF EXISTS article")
        conn.execute("PRAGMA user_version = 13")
        conn.commit()

        out = mgr.export_book(record.id, tmp_path / "old.buyn")
        assert bundle.read_manifest(out)["schema_version"] == 13

        dest = tmp_path / "restored.db"
        result = bundle.import_(out, dest)
        # Import brought it up to date and recreated the missing table.
        assert result["schema_version"] == S.SCHEMA_VERSION
        rconn = sqlite3.connect(str(dest))
        try:
            assert rconn.execute("PRAGMA user_version").fetchone()[0] == S.SCHEMA_VERSION
            assert rconn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='article'"
            ).fetchone() is not None
        finally:
            rconn.close()

        # And the restored book still opens + unlocks with the original passphrase.
        restored = mgr.import_book(out, tmp_path / "restored2.db", display_name="R2")
        s2 = mgr.open_book(restored.id, "pw")
        assert s2.connection().execute("SELECT COUNT(*) FROM verifikation").fetchone()[0] >= 1
