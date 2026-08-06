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

    def test_migrate_v25_adds_prefix_and_per_article_batches(self):
        # A v25 book: category without prefix + stock_batch with a GLOBAL-unique number.
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE verifikation (id INTEGER PRIMARY KEY);
            CREATE TABLE account (bas_konto INTEGER PRIMARY KEY, name TEXT, created_at TEXT);
            CREATE TABLE category (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, kind TEXT,
                bas_konto INTEGER, default_rate_code TEXT, active INTEGER DEFAULT 1, created_at TEXT);
            CREATE TABLE supplier (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);
            CREATE TABLE transaktion (id INTEGER PRIMARY KEY AUTOINCREMENT);
            CREATE TABLE article (id INTEGER PRIMARY KEY AUTOINCREMENT, article_number TEXT,
                description TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE stock_batch (id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_number INTEGER NOT NULL UNIQUE, article_id INTEGER NOT NULL,
                qty_in_centi INTEGER, qty_remaining_centi INTEGER, unit_cost_ore INTEGER,
                supplier_id INTEGER, purchase_transaktion_id INTEGER, received_date TEXT,
                note TEXT, created_at TEXT);
            CREATE TABLE customer (kundnummer INTEGER PRIMARY KEY AUTOINCREMENT);
            CREATE TABLE rut_claim (id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaktion_id INTEGER NOT NULL, customer_id INTEGER NOT NULL,
                rut_amount_ore INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                customer_payment_date TEXT, skatteverket_payment_date TEXT,
                skatteverket_verifikation_id INTEGER, skatteverket_received_ore INTEGER,
                shortfall_invoice_id INTEGER, skatteverket_reference TEXT,
                claim_year INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE invoice (id INTEGER PRIMARY KEY);   -- migration 31 ALTERs this
        """)
        db.execute("PRAGMA user_version = 25")
        db.execute("INSERT INTO account VALUES (3001,'x','t')")
        db.execute("INSERT INTO category(name,kind,bas_konto,created_at) VALUES ('A','income',3001,'t')")
        db.execute("INSERT INTO category(name,kind,bas_konto,created_at) VALUES ('B','expense',3001,'t')")
        db.execute("INSERT INTO article(article_number,description,created_at,updated_at) VALUES ('1000-1','R','t','t')")
        db.execute("INSERT INTO article(article_number,description,created_at,updated_at) VALUES ('1000-2','S','t','t')")
        # global batch numbers 1,2 on article 1; 3 on article 2
        for n, a, c in [(1, 1, 500), (2, 1, 600), (3, 2, 700)]:
            db.execute("INSERT INTO stock_batch(batch_number,article_id,qty_in_centi,"
                       "qty_remaining_centi,unit_cost_ore,received_date,created_at) "
                       "VALUES (?,?,100,100,?,'d','t')", (n, a, c))
        db.commit()
        assert S.migrate(db) == S.SCHEMA_VERSION
        prefixes = [r["prefix"] for r in db.execute("SELECT prefix FROM category ORDER BY id")]
        assert prefixes == ["0000", "0001"]
        # batches renumbered per article: article 1 -> 1,2 ; article 2 -> 1
        rows = {r["id"]: r["batch_number"] for r in db.execute(
            "SELECT id, batch_number FROM stock_batch")}
        assert rows == {1: 1, 2: 2, 3: 1}

    def test_migrate_v26_adds_category_parent_id(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        S.initialize_schema(db)
        db.execute("ALTER TABLE category DROP COLUMN parent_id")
        db.execute("PRAGMA user_version = 26")
        db.commit()
        assert S.migrate(db) == S.SCHEMA_VERSION
        cols = {r["name"] for r in db.execute("PRAGMA table_info(category)")}
        assert "parent_id" in cols

    def test_migrate_v27_makes_rut_claim_customer_nullable(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        S.initialize_schema(db)
        # rebuild rut_claim in the v27 shape (customer_id NOT NULL), then migrate
        db.execute("DROP TABLE rut_claim")
        db.execute("""CREATE TABLE rut_claim (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaktion_id INTEGER NOT NULL REFERENCES transaktion(id),
            customer_id INTEGER NOT NULL REFERENCES customer(kundnummer),
            rut_amount_ore INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            customer_payment_date TEXT, skatteverket_payment_date TEXT,
            skatteverket_verifikation_id INTEGER, skatteverket_received_ore INTEGER,
            shortfall_invoice_id INTEGER, skatteverket_reference TEXT,
            claim_year INTEGER NOT NULL, created_at TEXT NOT NULL)""")
        db.execute("PRAGMA user_version = 27")
        db.commit()
        assert S.migrate(db) == S.SCHEMA_VERSION
        info = {r["name"]: r for r in db.execute("PRAGMA table_info(rut_claim)")}
        assert info["customer_id"]["notnull"] == 0

    def test_migrate_v29_adds_expense_draft(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        S.initialize_schema(db)
        db.execute("DROP TABLE expense_draft")
        db.execute("PRAGMA user_version = 29")
        db.commit()
        assert S.migrate(db) == S.SCHEMA_VERSION
        names = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "expense_draft" in names

    def test_migrate_v32_adds_company_contact_and_invoice_column(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        S.initialize_schema(db)
        db.execute("DROP TABLE company_contact")
        db.execute("ALTER TABLE invoice DROP COLUMN contact_customer_id")
        db.execute("PRAGMA user_version = 31")
        db.commit()
        assert S.migrate(db) == S.SCHEMA_VERSION
        names = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "company_contact" in names
        cols = {r["name"] for r in db.execute("PRAGMA table_info(invoice)")}
        assert "contact_customer_id" in cols

    def test_migrate_v33_adds_delivery_address_column(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        S.initialize_schema(db)
        db.execute("ALTER TABLE invoice DROP COLUMN delivery_address_enc")
        db.execute("PRAGMA user_version = 32")
        db.commit()
        assert S.migrate(db) == S.SCHEMA_VERSION
        cols = {r["name"] for r in db.execute("PRAGMA table_info(invoice)")}
        assert "delivery_address_enc" in cols

    def test_migrate_is_idempotent(self, conn):
        before = S.get_schema_version(conn)
        assert S.migrate(conn) == before     # already current -> no-op
        assert S.migrate(conn) == before

    def test_migrate_adds_stock_batch_to_v24_db(self, tmp_path: Path):
        # Simulate a pre-inventory (v24) book: full schema minus stock_batch + the
        # invoice_line stock columns.
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        S.initialize_schema(db)
        db.execute("DROP TABLE stock_batch")
        db.execute("ALTER TABLE invoice_line RENAME COLUMN stock_batch_id TO _sbi_old")
        db.execute("ALTER TABLE invoice_line DROP COLUMN _sbi_old")
        db.execute("ALTER TABLE invoice_line DROP COLUMN cost_ore")
        db.execute("PRAGMA user_version = 24")
        db.commit()
        assert S.migrate(db) == S.SCHEMA_VERSION
        names = {r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "stock_batch" in names
        cols = {r["name"] for r in db.execute("PRAGMA table_info(invoice_line)")}
        assert {"stock_batch_id", "cost_ore"} <= cols


class TestInvoiceSchema:
    def test_new_tables_exist(self, conn):
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"company", "payment_method", "invoice", "invoice_line",
                "rut_recipient"} <= names

    def test_company_is_single_row(self, conn):
        conn.execute("INSERT INTO company(id, name) VALUES (1, 'Min Firma')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO company(id, name) VALUES (2, 'Annan')")

    def test_invoice_number_is_unique(self, conn):
        conn.execute("INSERT INTO invoice(invoice_number, invoice_date, due_date, created_at) "
                     "VALUES (1, '2026-01-01', '2026-01-31', '2026-01-01')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO invoice(invoice_number, invoice_date, due_date, created_at) "
                         "VALUES (1, '2026-02-01', '2026-02-28', '2026-02-01')")

    def test_v2_to_v3_migration_adds_invoice_tables(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        S.initialize_schema(db)
        for t in ("invoice", "invoice_line", "rut_recipient", "company", "payment_method"):
            db.execute(f"DROP TABLE {t}")
        db.execute("PRAGMA user_version = 2")
        db.commit()
        assert S.migrate(db) == S.SCHEMA_VERSION
        names = {r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"company", "payment_method", "invoice", "invoice_line", "rut_recipient"} <= names
