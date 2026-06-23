"""
Tests for Layer 4: core bookkeeping operations.

Exercises the full stack: encrypted book (L2) + schema (L3) + operations (L4).
Verifies double-entry balance, verifikationsnummer integrity, the kontantmetod
pending→paid flow, the RUT state machine, rättelse, and period locking.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from backend.db.manager import DatabaseManager
from backend.db.operations import (
    BookOps, InvalidState, PeriodLocked, compute_moms_figures,
)
from backend.models import schema as S


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def ops(tmp_path: Path) -> BookOps:
    mgr = DatabaseManager(app_dir=tmp_path / "app")
    _, session = mgr.create_book("Book", str(tmp_path / "book.db"), "pw")
    S.initialize_schema(session.connection())
    return BookOps(session)


def _postings(ops: BookOps, vid: int):
    return ops.conn.execute(
        "SELECT bas_konto, amount_ore FROM posting WHERE verifikation_id=? ORDER BY id", (vid,)
    ).fetchall()


def _balance(ops: BookOps, vid: int) -> int:
    return sum(p["amount_ore"] for p in _postings(ops, vid))


# ---------------------------------------------------------------------------
# Moms calculation
# ---------------------------------------------------------------------------

class TestMomsFigures:
    def test_inclusive_25(self):
        # 1250 öre incl 25% -> ex 1000, moms 250
        assert compute_moms_figures(1250, "25", True) == (1000, 250, 1250)

    def test_exclusive_25(self):
        assert compute_moms_figures(1000, "25", False) == (1000, 250, 1250)

    def test_figures_always_reconcile(self):
        ex, moms, inc = compute_moms_figures(999, "12", True)
        assert ex + moms == inc

    def test_momsfri_has_no_moms(self):
        assert compute_moms_figures(500, "momsfri", True) == (500, 0, 500)

    def test_ej_avdragsgill_folds_into_amount(self):
        assert compute_moms_figures(500, "ej_avdragsgill", True) == (500, 0, 500)


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

class TestReference:
    def test_create_category_autocreates_account(self, ops: BookOps):
        cid = ops.create_category("Kontorsmaterial", "expense", 5460)
        acct = ops.conn.execute("SELECT name FROM account WHERE bas_konto=5460").fetchone()
        assert acct is not None
        assert cid > 0

    def test_create_customer_validates_personnummer(self, ops: BookOps):
        with pytest.raises(ValueError):
            ops.create_customer("private", first_name="Anna", personnummer="811218-9875")

    def test_personnummer_stored_encrypted(self, ops: BookOps):
        kid = ops.create_customer("private", first_name="Anna", personnummer="811218-9876")
        raw = ops.conn.execute(
            "SELECT personnummer_enc FROM customer WHERE kundnummer=?", (kid,)
        ).fetchone()[0]
        assert raw != "811218-9876"
        assert ops.get_customer(kid)["personnummer"] == "811218-9876"

    def test_supplier_default_moms_rate(self, ops: BookOps):
        sid = ops.create_supplier("Inet")
        rate = ops.conn.execute(
            "SELECT default_moms_rate FROM supplier WHERE id=?", (sid,)
        ).fetchone()[0]
        assert rate == "25"

    def test_editing_customer_does_not_touch_snapshot(self, ops: BookOps):
        # snapshot-on-invoice: a later customer edit must not change the issued record
        cat = ops.create_category("Tjänst", "income", 3001)
        kid = ops.create_customer("private", first_name="Anna", last_name="A",
                                  personnummer="811218-9876")
        res = ops.record_income(kid, cat, [{"rate_code": "25", "amount_ore": 1250}],
                                "2026-03-01")
        ops.update_customer(kid, last_name="Changed")
        snap_enc = ops.conn.execute(
            "SELECT customer_snapshot_enc FROM transaktion WHERE id=?",
            (res["transaktion_id"],),
        ).fetchone()[0]
        import json
        snap = json.loads(ops.session.decrypt_text(snap_enc))
        assert snap["last_name"] == "A"  # frozen at issue


# ---------------------------------------------------------------------------
# Expense booking
# ---------------------------------------------------------------------------

class TestExpense:
    def test_cash_expense_books_balanced(self, ops: BookOps):
        cat = ops.create_category("Kontorsmaterial", "expense", 5460)
        res = ops.record_expense(None, cat, [{"rate_code": "25", "amount_ore": 1250}],
                                 "2026-02-01", paid_date="2026-02-01")
        assert "verifikation_id" in res
        assert _balance(ops, res["verifikation_id"]) == 0

    def test_expense_debits_expense_and_ingaende_moms(self, ops: BookOps):
        cat = ops.create_category("Kontorsmaterial", "expense", 5460)
        res = ops.record_expense(None, cat, [{"rate_code": "25", "amount_ore": 1250}],
                                 "2026-02-01", paid_date="2026-02-01")
        rows = {p["bas_konto"]: p["amount_ore"] for p in _postings(ops, res["verifikation_id"])}
        assert rows[5460] == 1000     # expense ex-moms (debit)
        assert rows[2640] == 250      # ingående moms (debit)
        assert rows[1930] == -1250    # bank (credit)

    def test_pending_expense_not_booked(self, ops: BookOps):
        cat = ops.create_category("X", "expense", 5460)
        res = ops.record_expense(None, cat, [{"rate_code": "25", "amount_ore": 1250}],
                                 "2026-02-01")
        assert "verifikation_id" not in res
        t = ops.conn.execute("SELECT status, verifikation_id FROM transaktion WHERE id=?",
                             (res["transaktion_id"],)).fetchone()
        assert t["status"] == "pending" and t["verifikation_id"] is None

    def test_wrong_category_kind_rejected(self, ops: BookOps):
        cat = ops.create_category("Sales", "income", 3001)
        with pytest.raises(ValueError):
            ops.record_expense(None, cat, [{"rate_code": "25", "amount_ore": 100}], "2026-02-01")


# ---------------------------------------------------------------------------
# Income booking
# ---------------------------------------------------------------------------

class TestIncome:
    def test_income_books_balanced_and_correct_accounts(self, ops: BookOps):
        cat = ops.create_category("Försäljning", "income", 3001)
        kid = ops.create_customer("business", company_name="ACME AB", org_nr="556000-0001")
        res = ops.record_income(kid, cat, [{"rate_code": "25", "amount_ore": 1250}],
                                "2026-03-01", paid_date="2026-03-10")
        rows = {p["bas_konto"]: p["amount_ore"] for p in _postings(ops, res["verifikation_id"])}
        assert _balance(ops, res["verifikation_id"]) == 0
        assert rows[1930] == 1250     # bank (debit)
        assert rows[3001] == -1000    # income (credit)
        assert rows[2610] == -250     # utgående moms 25% (credit)


# ---------------------------------------------------------------------------
# Verifikationsnummer integrity
# ---------------------------------------------------------------------------

class TestVerifikationsnummer:
    def test_sequential_unbroken(self, ops: BookOps):
        cat = ops.create_category("X", "expense", 5460)
        numbers = []
        for i in range(3):
            res = ops.record_expense(None, cat, [{"rate_code": "25", "amount_ore": 100}],
                                     "2026-02-01", paid_date="2026-02-01")
            numbers.append(res["ver_number"])
        assert numbers == [1, 2, 3]

    def test_double_booking_rejected(self, ops: BookOps):
        cat = ops.create_category("X", "expense", 5460)
        res = ops.record_expense(None, cat, [{"rate_code": "25", "amount_ore": 100}], "2026-02-01")
        ops.register_payment(res["transaktion_id"], "2026-02-02")
        with pytest.raises(InvalidState):
            ops.register_payment(res["transaktion_id"], "2026-02-03")


# ---------------------------------------------------------------------------
# RUT state machine
# ---------------------------------------------------------------------------

class TestRUT:
    def _rut_income(self, ops: BookOps):
        cat = ops.create_category("Städning", "income", 3001)
        kid = ops.create_customer("private", first_name="Anna", last_name="A",
                                  personnummer="811218-9876")
        # 10 000 kr inkl moms, 2 500 kr RUT
        res = ops.record_income(kid, cat, [{"rate_code": "25", "amount_ore": 1000000}],
                                "2026-04-01", rut_amount_ore=250000)
        return cat, kid, res

    def test_rut_requires_private_customer(self, ops: BookOps):
        cat = ops.create_category("Städning", "income", 3001)
        kid = ops.create_customer("business", company_name="ACME AB")
        with pytest.raises(ValueError):
            ops.record_income(kid, cat, [{"rate_code": "25", "amount_ore": 1000000}],
                              "2026-04-01", rut_amount_ore=250000)

    def test_rut_requires_personnummer(self, ops: BookOps):
        cat = ops.create_category("Städning", "income", 3001)
        kid = ops.create_customer("private", first_name="Anna")  # no pnr
        with pytest.raises(ValueError):
            ops.record_income(kid, cat, [{"rate_code": "25", "amount_ore": 1000000}],
                              "2026-04-01", rut_amount_ore=250000)

    def test_customer_payment_books_receivable(self, ops: BookOps):
        cat, kid, res = self._rut_income(ops)
        pay = ops.register_payment(res["transaktion_id"], "2026-04-15")
        rows = {p["bas_konto"]: p["amount_ore"] for p in _postings(ops, pay["verifikation_id"])}
        assert _balance(ops, pay["verifikation_id"]) == 0
        assert rows[1930] == 750000     # customer paid 7 500 (10 000 - 2 500 RUT)
        assert rows[1513] == 250000     # RUT receivable from Skatteverket
        claim = ops.conn.execute("SELECT state FROM rut_claim").fetchone()
        assert claim["state"] == "customer_paid"

    def test_skatteverket_payment_clears_receivable(self, ops: BookOps):
        cat, kid, res = self._rut_income(ops)
        ops.register_payment(res["transaktion_id"], "2026-04-15")
        claim_id = ops.conn.execute("SELECT id FROM rut_claim").fetchone()[0]
        sk = ops.register_rut_skatteverket_payment(claim_id, "2026-06-01")
        rows = {p["bas_konto"]: p["amount_ore"] for p in _postings(ops, sk["verifikation_id"])}
        assert rows[1930] == 250000     # Skatteverket pays the RUT part
        assert rows[1513] == -250000    # receivable cleared
        claim = ops.conn.execute("SELECT state, skatteverket_payment_date FROM rut_claim").fetchone()
        assert claim["state"] == "skatteverket_paid"
        assert claim["skatteverket_payment_date"] == "2026-06-01"

    def test_cannot_skip_to_skatteverket_paid(self, ops: BookOps):
        cat, kid, res = self._rut_income(ops)
        claim_id = ops.conn.execute("SELECT id FROM rut_claim").fetchone()[0]
        with pytest.raises(InvalidState):
            ops.register_rut_skatteverket_payment(claim_id, "2026-06-01")

    def test_cap_status_tracks_usage(self, ops: BookOps):
        cat, kid, res = self._rut_income(ops)
        status = ops.rut_cap_status(kid, 2026)
        assert status["used_ore"] == 250000
        assert status["remaining_ore"] == status["cap_ore"] - 250000


# ---------------------------------------------------------------------------
# Rättelse (correction)
# ---------------------------------------------------------------------------

class TestRattelse:
    def test_reverse_creates_mirror_referencing_original(self, ops: BookOps):
        cat = ops.create_category("X", "expense", 5460)
        res = ops.record_expense(None, cat, [{"rate_code": "25", "amount_ore": 1250}],
                                 "2026-02-01", paid_date="2026-02-01")
        orig_vid = res["verifikation_id"]
        rev = ops.reverse_verifikation(orig_vid, "fel konto")

        # mirror nets the original to zero
        orig = {p["bas_konto"]: p["amount_ore"] for p in _postings(ops, orig_vid)}
        mirror = {p["bas_konto"]: p["amount_ore"] for p in _postings(ops, rev["verifikation_id"])}
        for konto in orig:
            assert orig[konto] == -mirror[konto]

        link = ops.conn.execute(
            "SELECT rattelse_of FROM verifikation WHERE id=?", (rev["verifikation_id"],)
        ).fetchone()[0]
        assert link == orig_vid

    def test_original_still_present_after_rattelse(self, ops: BookOps):
        cat = ops.create_category("X", "expense", 5460)
        res = ops.record_expense(None, cat, [{"rate_code": "25", "amount_ore": 1250}],
                                 "2026-02-01", paid_date="2026-02-01")
        ops.reverse_verifikation(res["verifikation_id"], "fel")
        still = ops.conn.execute(
            "SELECT COUNT(*) FROM verifikation WHERE id=?", (res["verifikation_id"],)
        ).fetchone()[0]
        assert still == 1


# ---------------------------------------------------------------------------
# Period locking
# ---------------------------------------------------------------------------

class TestPeriodLock:
    def test_booking_into_locked_period_rejected(self, ops: BookOps):
        cat = ops.create_category("X", "expense", 5460)
        ops.lock_period("2026-01-01", "2026-03-31", "moms")
        res = ops.record_expense(None, cat, [{"rate_code": "25", "amount_ore": 100}], "2026-02-01")
        with pytest.raises(PeriodLocked):
            ops.register_payment(res["transaktion_id"], "2026-02-15")

    def test_booking_outside_locked_period_ok(self, ops: BookOps):
        cat = ops.create_category("X", "expense", 5460)
        ops.lock_period("2026-01-01", "2026-03-31", "moms")
        res = ops.record_expense(None, cat, [{"rate_code": "25", "amount_ore": 100}],
                                 "2026-04-01", paid_date="2026-04-01")
        assert "verifikation_id" in res

    def test_is_period_locked(self, ops: BookOps):
        ops.lock_period("2026-01-01", "2026-03-31")
        assert ops.is_period_locked("2026-02-15")
        assert not ops.is_period_locked("2026-04-15")
