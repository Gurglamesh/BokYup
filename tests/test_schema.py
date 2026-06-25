"""
Tests for Layer 3: schema.

Uses Layer 2 (DatabaseManager) to get a real encrypted book + connection, then
verifies the schema, its constraints, the DB-level immutability triggers, and the
validation/money helpers.
"""

from __future__ import annotations

import sqlite3
import pytest
from pathlib import Path

from backend.db.manager import DatabaseManager
from backend.models import schema as S


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path: Path):
    """A fresh, schema-initialized connection on a real encrypted book."""
    mgr = DatabaseManager(app_dir=tmp_path / "app")
    _, session = mgr.create_book("Book", str(tmp_path / "book.db"), "pw")
    c = session.connection()
    S.initialize_schema(c)
    return c


def _today():
    return "2026-06-01"


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

class TestInitialize:
    def test_sets_schema_version(self, conn):
        assert S.get_schema_version(conn) == S.SCHEMA_VERSION

    def test_is_initialized(self, conn):
        assert S.is_initialized(conn)

    def test_all_tables_exist(self, conn):
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r[0] for r in rows}
        expected = {
            "meta", "config", "account", "category", "customer", "supplier",
            "verifikation", "posting", "transaktion", "moms_line",
            "rut_claim", "period_lock",
        }
        assert expected <= names

    def test_default_config_seeded(self, conn):
        row = conn.execute(
            "SELECT value FROM config WHERE key='rut_rot_cap_ore_per_customer_year'"
        ).fetchone()
        assert row is not None and int(row[0]) > 0

    def test_fresh_db_not_initialized(self, tmp_path):
        mgr = DatabaseManager(app_dir=tmp_path / "app2")
        _, session = mgr.create_book("B2", str(tmp_path / "b2.db"), "pw")
        assert not S.is_initialized(session.connection())


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------

class TestConstraints:
    def _account(self, conn, n=1930, name="Bank"):
        conn.execute(
            "INSERT INTO account(bas_konto, name, created_at) VALUES (?,?,?)",
            (n, name, _today()),
        )

    def test_verifikationsnummer_unique_per_series(self, conn):
        conn.execute(
            "INSERT INTO verifikation(series, ver_number, ver_date, "
            "registration_date, text, posted, created_at) "
            "VALUES ('A', 1, ?, ?, 'x', 1, ?)",
            (_today(), _today(), _today()),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO verifikation(series, ver_number, ver_date, "
                "registration_date, text, posted, created_at) "
                "VALUES ('A', 1, ?, ?, 'x', 1, ?)",
                (_today(), _today(), _today()),
            )

    def test_multiple_draft_nulls_allowed(self, conn):
        # Drafts have ver_number = NULL and must not collide under UNIQUE.
        for _ in range(3):
            conn.execute(
                "INSERT INTO verifikation(ver_date, registration_date, text, created_at) "
                "VALUES (?,?,'draft',?)",
                (_today(), _today(), _today()),
            )
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM verifikation").fetchone()[0]
        assert n == 3

    def test_foreign_key_enforced(self, conn):
        # category references a non-existent account
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO category(name, kind, bas_konto, created_at) "
                "VALUES ('Office', 'expense', 9999, ?)",
                (_today(),),
            )
            conn.commit()

    def test_moms_rate_check_constraint(self, conn):
        conn.execute(
            "INSERT INTO transaktion(direction, trans_date, created_at) "
            "VALUES ('in', ?, ?)",
            (_today(), _today()),
        )
        tid = conn.execute("SELECT id FROM transaktion").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO moms_line(transaktion_id, rate_code, ex_moms_ore, "
                "moms_ore, inc_moms_ore) VALUES (?, '99', 100, 25, 125)",
                (tid,),
            )
            conn.commit()

    def test_customer_type_check_constraint(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO customer(type, created_at) VALUES ('alien', ?)",
                (_today(),),
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Immutability triggers (the legal backstop)
# ---------------------------------------------------------------------------

class TestImmutability:
    def _posted_ver(self, conn):
        conn.execute(
            "INSERT INTO verifikation(series, ver_number, ver_date, "
            "registration_date, text, posted, created_at) "
            "VALUES ('A', 1, ?, ?, 'sale', 1, ?)",
            (_today(), _today(), _today()),
        )
        conn.commit()
        return conn.execute("SELECT id FROM verifikation").fetchone()[0]

    def test_cannot_update_posted_verifikation(self, conn):
        vid = self._posted_ver(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE verifikation SET text='hacked' WHERE id=?", (vid,))

    def test_cannot_delete_posted_verifikation(self, conn):
        vid = self._posted_ver(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM verifikation WHERE id=?", (vid,))

    def test_can_post_a_draft(self, conn):
        # Flipping posted 0 -> 1 and assigning a number must be allowed.
        conn.execute(
            "INSERT INTO verifikation(ver_date, registration_date, text, created_at) "
            "VALUES (?,?,'draft',?)",
            (_today(), _today(), _today()),
        )
        vid = conn.execute("SELECT id FROM verifikation").fetchone()[0]
        conn.execute(
            "UPDATE verifikation SET posted=1, ver_number=1 WHERE id=?", (vid,)
        )
        conn.commit()
        row = conn.execute("SELECT posted, ver_number FROM verifikation WHERE id=?", (vid,)).fetchone()
        assert row[0] == 1 and row[1] == 1

    def test_can_edit_draft_freely(self, conn):
        conn.execute(
            "INSERT INTO verifikation(ver_date, registration_date, text, created_at) "
            "VALUES (?,?,'draft',?)",
            (_today(), _today(), _today()),
        )
        vid = conn.execute("SELECT id FROM verifikation").fetchone()[0]
        conn.execute("UPDATE verifikation SET text='edited' WHERE id=?", (vid,))
        conn.commit()  # no trigger fires while posted = 0

    def test_cannot_modify_posted_postings(self, conn):
        vid = self._posted_ver(conn)
        conn.execute("INSERT INTO account(bas_konto, name, created_at) VALUES (1930,'Bank',?)", (_today(),))
        # Inserting a posting under a posted ver is itself fine; modifying it is not.
        conn.execute(
            "INSERT INTO posting(verifikation_id, bas_konto, amount_ore) VALUES (?,1930,1000)",
            (vid,),
        )
        conn.commit()
        pid = conn.execute("SELECT id FROM posting").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE posting SET amount_ore=9999 WHERE id=?", (pid,))


# ---------------------------------------------------------------------------
# Encrypted-column round trip via the session
# ---------------------------------------------------------------------------

class TestEncryptedColumns:
    def test_personnummer_stored_encrypted_and_roundtrips(self, tmp_path):
        mgr = DatabaseManager(app_dir=tmp_path / "app")
        _, session = mgr.create_book("B", str(tmp_path / "b.db"), "pw")
        conn = session.connection()
        S.initialize_schema(conn)

        pnr = "811218-9876"
        enc = session.encrypt_text(pnr)
        conn.execute(
            "INSERT INTO customer(type, first_name, personnummer_enc, created_at) "
            "VALUES ('private', 'Anna', ?, ?)",
            (enc, _today()),
        )
        conn.commit()

        stored = conn.execute("SELECT personnummer_enc FROM customer").fetchone()[0]
        assert stored != pnr  # ciphertext, not plaintext
        assert session.decrypt_text(stored) == pnr


# ---------------------------------------------------------------------------
# Validation & money helpers
# ---------------------------------------------------------------------------

class TestPersonnummer:
    @pytest.mark.parametrize("pnr", ["811218-9876", "8112189876", "19811218-9876"])
    def test_valid(self, pnr):
        assert S.is_valid_personnummer(pnr)

    @pytest.mark.parametrize("pnr", ["811218-9875", "123", "", "abcd", "811218-9870"])
    def test_invalid(self, pnr):
        assert not S.is_valid_personnummer(pnr)

    def test_normalize(self):
        assert S.normalize_personnummer("19811218-9876") == "8112189876"


class TestMoney:
    @pytest.mark.parametrize("kr,ore", [("12.50", 1250), (0, 0), ("99.99", 9999), (100, 10000)])
    def test_kronor_to_ore(self, kr, ore):
        assert S.kronor_to_ore(kr) == ore

    def test_round_half_up(self):
        assert S.kronor_to_ore("0.005") == 1  # 0.5 öre rounds up

    def test_roundtrip(self):
        assert str(S.ore_to_kronor(1250)) == "12.50"


class TestMomsRate:
    def test_known_rates(self):
        assert S.moms_rate("25") == __import__("decimal").Decimal("0.25")
        assert S.moms_rate("momsfri") is None
        assert S.moms_rate("ej_avdragsgill") is None

    def test_unknown_rate_raises(self):
        with pytest.raises(ValueError):
            S.moms_rate("18")


# ---------------------------------------------------------------------------
# Forward migration (existing books gain new tables)
# ---------------------------------------------------------------------------

class TestMigration:
    def test_fresh_schema_is_current(self, conn):
        assert S.get_schema_version(conn) == S.SCHEMA_VERSION
        assert S.SCHEMA_VERSION >= 2

    def test_migrate_adds_receipt_table_to_v1_db(self, tmp_path: Path):
        # Simulate a pre-receipt (v1) book: full schema minus the receipt table.
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        S.initialize_schema(db)
        db.execute("DROP TABLE receipt")
        db.execute("PRAGMA user_version = 1")
        db.commit()
        assert S.get_schema_version(db) == 1

        result = S.migrate(db)
        assert result == S.SCHEMA_VERSION
        assert S.get_schema_version(db) == S.SCHEMA_VERSION
        # receipt table now exists and is usable
        names = {r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "receipt" in names

    def test_migrate_is_idempotent(self, conn):
        before = S.get_schema_version(conn)
        assert S.migrate(conn) == before     # already current -> no-op
        assert S.migrate(conn) == before
