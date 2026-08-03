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

    # ---- Skatteverket payout: manual amount, rounding (3740) + partial payout -----

    def _rut_invoice(self, ops: BookOps, labour_inc=1000000):
        """A RUT invoice whose customer has paid; returns (invoice_id, claim_id, husavdrag)."""
        cat = ops.create_category("Städning", "income", 3001, default_rate_code="25")
        kid = ops.create_customer("private", first_name="Anna", last_name="A",
                                  personnummer="811218-9876")
        ex = round(labour_inc / 1.25)
        inv = ops.create_invoice(
            customer_id=kid, invoice_date="2026-04-01", due_date="2026-04-30",
            lines=[{"description": "Städ", "quantity_centi": 100, "unit_price_ore": ex,
                    "rate_code": "25", "category_id": cat, "reduction_type": "rut"}],
            recipients=[{"customer_id": kid, "rut_share_pct": 100, "rot_share_pct": 100}])
        ops.register_payment(inv["transaktion_id"], "2026-04-15")
        claim_id = ops.conn.execute("SELECT id FROM rut_claim WHERE transaktion_id=?",
                                    (inv["transaktion_id"],)).fetchone()[0]
        husavdrag = inv["rut_total_ore"] + inv["rot_total_ore"]
        return inv["invoice_id"], claim_id, husavdrag

    def test_skatteverket_rounding_booked_to_3740(self, ops: BookOps):
        _, claim_id, H = self._rut_invoice(ops)
        sk = ops.register_rut_skatteverket_payment(claim_id, "2026-06-01", received_ore=H - 37)
        rows = {p["bas_konto"]: p["amount_ore"] for p in _postings(ops, sk["verifikation_id"])}
        assert _balance(ops, sk["verifikation_id"]) == 0
        assert sk["interpretation"] == "rounding"
        assert rows[1930] == H - 37          # actual cash in
        assert rows[1513] == -H              # receivable fully cleared
        assert rows[3740] == 37              # the öre diff
        assert sk["shortfall_invoice_id"] is None

    def test_skatteverket_overpayment_within_tolerance_rounds(self, ops: BookOps):
        _, claim_id, H = self._rut_invoice(ops)
        sk = ops.register_rut_skatteverket_payment(claim_id, "2026-06-01", received_ore=H + 20)
        rows = {p["bas_konto"]: p["amount_ore"] for p in _postings(ops, sk["verifikation_id"])}
        assert _balance(ops, sk["verifikation_id"]) == 0
        assert rows[3740] == -20             # other direction

    def test_skatteverket_partial_requires_explicit_confirm(self, ops: BookOps):
        _, claim_id, H = self._rut_invoice(ops)
        with pytest.raises(InvalidState):
            ops.register_rut_skatteverket_payment(claim_id, "2026-06-01", received_ore=H - 50000)

    def test_skatteverket_partial_creates_followup_invoice(self, ops: BookOps):
        inv_id, claim_id, H = self._rut_invoice(ops)
        sk = ops.register_rut_skatteverket_payment(
            claim_id, "2026-06-01", received_ore=H - 50000, mode="partial")
        rows = {p["bas_konto"]: p["amount_ore"] for p in _postings(ops, sk["verifikation_id"])}
        assert _balance(ops, sk["verifikation_id"]) == 0
        assert sk["interpretation"] == "partial"
        assert rows[1930] == H - 50000       # actual payout
        assert rows[1513] == -H              # SKV receivable cleared
        assert rows[1510] == 50000           # remainder now owed by the customer
        # A linked follow-up invoice documents the shortfall, no moms, same series.
        fi_id = sk["shortfall_invoice_id"]
        assert fi_id is not None
        fi = ops.get_invoice(fi_id)
        assert fi["parent_invoice_id"] == inv_id
        assert fi["husavdrag_shortfall_ore"] == 50000
        assert fi["moms_ore"] == 0
        assert fi["inc_moms_ore"] == 50000
        assert str(ops.get_invoice(inv_id)["invoice_number"]) in fi["relation_note"]

    def test_followup_invoice_payment_clears_1510(self, ops: BookOps):
        _, claim_id, H = self._rut_invoice(ops)
        sk = ops.register_rut_skatteverket_payment(
            claim_id, "2026-06-01", received_ore=H - 50000, mode="partial")
        fi_id = sk["shortfall_invoice_id"]
        pay = ops.pay_invoice(fi_id, date="2026-07-01")
        rows = {p["bas_konto"]: p["amount_ore"] for p in _postings(ops, pay["verifikation_id"])}
        assert _balance(ops, pay["verifikation_id"]) == 0
        assert rows[1930] == 50000           # customer pays the remainder
        assert rows[1510] == -50000          # customer receivable cleared, no moms touched
        assert pay["outstanding_ore"] == 0
        # Crediting/refunding a husavdrag follow-up is not supported.
        with pytest.raises(InvalidState):
            ops.credit_invoice(fi_id)

    def test_skatteverket_overpaid_beyond_tolerance_refused(self, ops: BookOps):
        _, claim_id, H = self._rut_invoice(ops)
        with pytest.raises(InvalidState):
            ops.register_rut_skatteverket_payment(claim_id, "2026-06-01", received_ore=H + 100000)

    def test_skatteverket_preview_interpretations(self, ops: BookOps):
        _, claim_id, H = self._rut_invoice(ops)
        assert ops.skatteverket_payment_preview(claim_id, H)["interpretation"] == "exact"
        assert ops.skatteverket_payment_preview(claim_id, H - 30)["interpretation"] == "rounding"
        assert ops.skatteverket_payment_preview(claim_id, H - 90000)["interpretation"] == "partial"
        assert ops.skatteverket_payment_preview(claim_id, H + 90000)["interpretation"] == "overpaid"

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


# ---------------------------------------------------------------------------
# Rättelse now nets the moms/result reports (item 3a)
# ---------------------------------------------------------------------------

class TestRattelseNetting:
    def _sale_paid(self, ops):
        cat = ops.create_category("Försäljning", "income", 3001)
        kid = ops.create_customer("business", company_name="ACME AB")
        return ops.record_income(kid, cat, [{"rate_code": "25", "amount_ore": 1250}],
                                 "2026-02-10", paid_date="2026-02-10")

    def test_reversed_sale_nets_momsdeklaration(self, ops: BookOps):
        from backend.reports import vat
        res = self._sale_paid(ops)
        before = vat.momsdeklaration(ops.conn, "2026-01-01", "2026-03-31")
        assert before["boxes"]["10"] == 250
        ops.reverse_verifikation(res["verifikation_id"], "makulerad", reg_date="2026-02-20")
        after = vat.momsdeklaration(ops.conn, "2026-01-01", "2026-03-31")
        assert after["boxes"]["10"] == 0      # output VAT netted by the rättelse
        assert after["boxes"]["05"] == 0      # sales base netted too

    def test_reversed_sale_nets_result(self, ops: BookOps):
        from backend.reports import result
        res = self._sale_paid(ops)
        ops.reverse_verifikation(res["verifikation_id"], "makulerad", reg_date="2026-02-20")
        rep = result.result_report(ops.conn, "2026-01-01", "2026-03-31")
        assert rep["income_ore"] == 0


# ---------------------------------------------------------------------------
# Year-end accrual (bokslut) — item 2
# ---------------------------------------------------------------------------

class TestYearEndAccrual:
    def _pending_sale(self, ops, date="2026-12-20"):
        cat = ops.create_category("Försäljning", "income", 3001)
        kid = ops.create_customer("business", company_name="ACME AB")
        return ops.record_income(kid, cat, [{"rate_code": "25", "amount_ore": 1250}], date)

    def test_accrual_books_unpaid_invoice_balanced(self, ops: BookOps):
        self._pending_sale(ops)
        out = ops.book_year_end_accruals("2026-12-31")
        assert out["count"] == 1
        acc = out["accruals"][0]
        avid = ops.conn.execute("SELECT id FROM verifikation WHERE ver_number=?",
                                (acc["accrual_ver"],)).fetchone()[0]
        rows = {p["bas_konto"]: p["amount_ore"] for p in _postings(ops, avid)}
        assert _balance(ops, avid) == 0
        assert rows[1510] == 1250     # kundfordran (debit)
        assert rows[3001] == -1000    # income (credit)
        assert rows[2610] == -250     # utgående moms (credit)

    def test_moms_lands_in_closing_year_and_nets_next_year(self, ops: BookOps):
        from backend.reports import vat
        res = self._pending_sale(ops)
        ops.book_year_end_accruals("2026-12-31")
        # moms reported in the closing year (accrual)
        y2026 = vat.momsdeklaration(ops.conn, "2026-01-01", "2026-12-31")
        assert y2026["boxes"]["10"] == 250
        # in the new year, the reversal nets the eventual cash payment
        ops.register_payment(res["transaktion_id"], "2027-01-15")
        q1_2027 = vat.momsdeklaration(ops.conn, "2027-01-01", "2027-03-31")
        assert q1_2027["boxes"]["10"] == 0   # reversal (−250) + payment (+250)

    def test_only_pending_on_or_before_year_end(self, ops: BookOps):
        cat = ops.create_category("Försäljning", "income", 3001)
        kid = ops.create_customer("business", company_name="ACME AB")
        ops.record_income(kid, cat, [{"rate_code": "25", "amount_ore": 1250}], "2027-01-05")
        out = ops.book_year_end_accruals("2026-12-31")
        assert out["count"] == 0   # invoice dated next year is not accrued

    def test_paid_invoices_not_accrued(self, ops: BookOps):
        cat = ops.create_category("Försäljning", "income", 3001)
        kid = ops.create_customer("business", company_name="ACME AB")
        ops.record_income(kid, cat, [{"rate_code": "25", "amount_ore": 1250}],
                          "2026-12-20", paid_date="2026-12-20")
        out = ops.book_year_end_accruals("2026-12-31")
        assert out["count"] == 0


# ---------------------------------------------------------------------------
# Receipts (encrypted photos)
# ---------------------------------------------------------------------------

class TestReceipts:
    def _pending_expense(self, ops: BookOps) -> int:
        cat = ops.create_category("Kontorsmaterial", "expense", 5460)
        res = ops.record_expense(None, cat, [{"rate_code": "25", "amount_ore": 1250}],
                                 "2026-02-01")
        return res["transaktion_id"]

    def test_attach_then_get_roundtrips(self, ops: BookOps):
        tid = self._pending_expense(ops)
        data = b"\x89PNG\r\n\x1a\n fake image bytes \xff\x00"
        rc = ops.attach_receipt(tid, data, "image/png", "paper")
        got, mime = ops.get_receipt(rc["id"])
        assert got == data and mime == "image/png"

    def test_stored_file_is_ciphertext(self, ops: BookOps, tmp_path: Path):
        tid = self._pending_expense(ops)
        data = b"secret receipt total 1250"
        rc = ops.attach_receipt(tid, data, "image/jpeg")
        photos = Path(str(ops.session.record.db_path) + ".photos")
        blob = (photos / rc["filename"]).read_bytes()
        assert data not in blob          # encrypted at rest
        assert len(blob) > len(data)     # nonce + GCM tag overhead

    def test_list_receipts(self, ops: BookOps):
        tid = self._pending_expense(ops)
        ops.attach_receipt(tid, b"a", "image/png")
        ops.attach_receipt(tid, b"bb", "image/png")
        lst = ops.list_receipts(tid)
        assert len(lst) == 2
        assert {r["byte_size"] for r in lst} == {1, 2}

    def test_integrity_check_detects_tampering(self, ops: BookOps):
        tid = self._pending_expense(ops)
        rc = ops.attach_receipt(tid, b"hello", "image/png")
        photos = Path(str(ops.session.record.db_path) + ".photos")
        (photos / rc["filename"]).write_bytes(b"tampered")
        with pytest.raises(Exception):
            ops.get_receipt(rc["id"])

    def test_delete_allowed_while_pending(self, ops: BookOps):
        tid = self._pending_expense(ops)
        rc = ops.attach_receipt(tid, b"x", "image/png")
        ops.delete_receipt(rc["id"])
        assert ops.list_receipts(tid) == []

    def test_delete_blocked_after_booking(self, ops: BookOps):
        tid = self._pending_expense(ops)
        rc = ops.attach_receipt(tid, b"x", "image/png")
        ops.register_payment(tid, "2026-02-05")     # books it -> immutable
        with pytest.raises(InvalidState):
            ops.delete_receipt(rc["id"])

    def test_attach_rejects_unknown_transaktion(self, ops: BookOps):
        with pytest.raises(KeyError):
            ops.attach_receipt(9999, b"x", "image/png")


# ---------------------------------------------------------------------------
# Invoices (faktura)
# ---------------------------------------------------------------------------

class TestInvoices:
    def _setup(self, ops):
        cat = ops.create_category("Tjänster", "income", 3001)
        kid = ops.create_customer("private", first_name="Anna", last_name="Svensson",
                                  personnummer="811218-9876", address="Storgatan 1, Stockholm")
        return cat, kid

    def test_company_and_payment_methods(self, ops):
        ops.set_company(name="Min Firma AB", org_nr="556677-8899", vat_nr="SE556677889901",
                        address="Vägen 2", f_skatt=1)
        assert ops.get_company()["name"] == "Min Firma AB"
        pid = ops.create_payment_method("Swish", "123 456 78 90")
        ops.create_payment_method("Bankgiro", "123-4567", sort_order=1)
        methods = ops.list_payment_methods(active_only=True)
        assert [m["label"] for m in methods] == ["Swish", "Bankgiro"]
        ops.update_payment_method(pid, active=0)
        assert [m["label"] for m in ops.list_payment_methods(active_only=True)] == ["Bankgiro"]

    def test_create_invoice_numbering_and_lines(self, ops):
        cat, kid = self._setup(ops)
        inv = ops.create_invoice(
            customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Konsult", "quantity_centi": 200, "unit": "h",
                    "unit_price_ore": 100000, "rate_code": "25"},
                   {"description": "Material", "quantity_centi": 100, "unit": "st",
                    "unit_price_ore": 5000, "rate_code": "25"}])
        # 2h*1000 + 1*50 = 2050 kr ex; moms 25% = 512.50 kr
        assert inv["invoice_number"] == 1
        assert inv["ex_moms_ore"] == 205000
        assert inv["moms_ore"] == 51250
        assert inv["inc_moms_ore"] == 256250
        # next invoice gets the next unbroken number
        inv2 = ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-02",
                                  due_date="2026-04-01",
                                  lines=[{"description": "X", "quantity_centi": 100,
                                          "unit_price_ore": 10000, "rate_code": "25"}])
        assert inv2["invoice_number"] == 2

    def test_support_minutes_earned_and_expiry(self, ops):
        cat, kid = self._setup(ops)
        # inc = 124 900 öre (1 249 kr) -> floor(1249/500)=2 -> 30 min; expiry +36 months
        ex = round(124900 / 1.25)
        inv = ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-15",
            due_date="2026-04-15",
            lines=[{"description": "IT", "quantity_centi": 100, "unit_price_ore": ex, "rate_code": "25"}])
        assert inv["inc_moms_ore"] == 124900
        assert inv["support_minutes_earned"] == 30
        assert inv["support_expiry_date"] == "2029-03-15"
        got = ops.get_invoice(inv["invoice_id"])
        assert got["support_minutes_earned"] == 30 and got["support_expiry_date"] == "2029-03-15"

    def test_support_balance_expiry_and_ledger(self, ops):
        cat, kid = self._setup(ops)
        # two active invoices: 30 min + 15 min = 45 earned
        ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-15",
            due_date="2026-04-15",
            lines=[{"description": "A", "quantity_centi": 100, "unit_price_ore": round(124900/1.25), "rate_code": "25"}])
        ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-04-01",
            due_date="2026-05-01",
            lines=[{"description": "B", "quantity_centi": 100, "unit_price_ore": round(60000/1.25), "rate_code": "25"}])
        assert ops.support_balance(kid)["remaining_minutes"] == 45
        # deduct 15 + 30, add 10 -> net used 35 -> remaining 10
        ops.record_support_entry(kid, 15, "deduction", "Felsökning")
        ops.record_support_entry(kid, 30, "deduction")
        ops.record_support_entry(kid, 10, "addition", "Goodwill")
        bal = ops.support_balance(kid)
        assert bal["used_minutes"] == 35 and bal["remaining_minutes"] == 10
        assert len(ops.list_support_ledger(kid)) == 3
        # an expired invoice drops out of the active earned sum
        ops.conn.execute("UPDATE invoice SET support_expiry_date='2020-01-01' "
                         "WHERE invoice_number=1")
        ops.conn.commit()
        # now only the 15-min invoice is active; earned 15, used 35 -> remaining floors at 0
        bal2 = ops.support_balance(kid)
        assert bal2["earned_active_minutes"] == 15 and bal2["used_minutes"] == 35
        assert bal2["remaining_minutes"] == 0

    def test_makulerad_invoice_earns_no_support(self, ops):
        cat, kid = self._setup(ops)
        inv = ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-15",
            due_date="2026-04-15",
            lines=[{"description": "A", "quantity_centi": 100, "unit_price_ore": round(124900/1.25), "rate_code": "25"}])
        assert ops.support_balance(kid)["earned_active_minutes"] == 30
        ops.cancel_invoice(inv["invoice_id"])          # makulera -> no support time
        assert ops.support_balance(kid)["earned_active_minutes"] == 0

    def test_support_entry_validation(self, ops):
        cat, kid = self._setup(ops)
        with pytest.raises(ValueError):
            ops.record_support_entry(kid, 0, "deduction")
        with pytest.raises(ValueError):
            ops.record_support_entry(kid, 15, "bogus")

    def test_line_percentage_discount(self, ops):
        cat, kid = self._setup(ops)
        # qty 2 * 1000 kr = 2000 kr ex; 15 % rabatt -> 1700 kr ex; moms 25 % = 425 kr
        inv = ops.create_invoice(
            customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Konsult", "quantity_centi": 200, "unit_price_ore": 100000,
                    "rate_code": "25", "discount_pct_centi": 1500}])
        assert inv["ex_moms_ore"] == 170000
        assert inv["moms_ore"] == 42500
        assert inv["inc_moms_ore"] == 212500
        line = ops.get_invoice(inv["invoice_id"])["lines"][0]
        assert line["discount_pct_centi"] == 1500
        assert line["unit_price_ore"] == 100000        # list price kept for the PDF
        assert line["ex_moms_ore"] == 170000           # stored discounted

    def test_discount_out_of_range_rejected(self, ops):
        cat, kid = self._setup(ops)
        with pytest.raises(ValueError):
            ops.create_invoice(
                customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
                lines=[{"description": "x", "quantity_centi": 100, "unit_price_ore": 1000,
                        "rate_code": "25", "discount_pct_centi": 12000}])

    def test_discount_on_rut_line_reduces_husavdrag(self, ops):
        cat = ops.create_category("Städ", "income", 3001)
        kid = ops.create_customer("private", first_name="Anna", last_name="A",
                                  personnummer="811218-9876")
        # 10 000 kr ex labour, 20 % rabatt -> 8 000 ex -> 10 000 inc moms; RUT 50 % = 5 000
        inv = ops.create_invoice(
            customer_id=kid, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Städ", "quantity_centi": 100, "unit_price_ore": 1000000,
                    "rate_code": "25", "category_id": cat, "reduction_type": "rut",
                    "discount_pct_centi": 2000}],
            recipients=[{"customer_id": kid, "rut_share_pct": 100}])
        assert inv["ex_moms_ore"] == 800000
        assert inv["rut_total_ore"] == 500000          # 50 % of the discounted inc-moms labour

    def test_invoice_is_pending_then_books_when_paid(self, ops):
        cat, kid = self._setup(ops)
        inv = ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-01",
                                 due_date="2026-03-31",
                                 lines=[{"description": "Jobb", "quantity_centi": 100,
                                         "unit_price_ore": 100000, "rate_code": "25"}])
        got = ops.get_invoice(inv["invoice_id"])
        assert got["status"] == "pending"
        ops.register_payment(inv["transaktion_id"], "2026-03-15")
        assert ops.get_invoice(inv["invoice_id"])["status"] == "paid"

    def test_rut_split_across_household(self, ops):
        cat, kid = self._setup(ops)
        # ex 1 000 000 @ 25% -> inc 1 250 000; RUT pot = 50% incl moms = 625 000.
        inv = ops.create_invoice(
            customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Städning", "quantity_centi": 100, "unit_price_ore": 1000000,
                    "rate_code": "25", "reduction_type": "rut"}],
            recipients=[{"first_name": "Anna", "last_name": "Svensson",
                         "personnummer": "811218-9876", "share_pct": 60},
                        {"first_name": "Björn", "last_name": "Svensson",
                         "personnummer": "19811218-9876", "share_pct": 40}])
        assert inv["rut_total_ore"] == 625000
        got = ops.get_invoice(inv["invoice_id"])
        assert len(got["recipients"]) == 2
        assert got["recipients"][0]["personnummer"] == "8112189876"   # normalized + decrypted
        assert got["recipients"][0]["rut_amount_ore"] == 375000        # 60 %
        assert got["recipients"][1]["rut_amount_ore"] == 250000        # 40 %
        assert sum(r["rut_amount_ore"] for r in got["recipients"]) == 625000

    def test_invoice_rejects_bad_recipient_personnummer(self, ops):
        cat, kid = self._setup(ops)
        with pytest.raises(ValueError):
            ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-01",
                               due_date="2026-03-31",
                               lines=[{"description": "X", "quantity_centi": 100,
                                       "unit_price_ore": 1000, "rate_code": "25"}],
                               recipients=[{"first_name": "A", "last_name": "B",
                                            "personnummer": "811218-9875",  # bad Luhn
                                            "rut_amount_ore": 100}])

    def test_mixed_rut_and_rot_pots(self, ops):
        cat, kid = self._setup(ops)
        inv = ops.create_invoice(
            customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Städ", "quantity_centi": 100, "unit_price_ore": 1000000,
                    "rate_code": "25", "reduction_type": "rut"},          # inc 1 250 000 -> RUT 625 000
                   {"description": "Snickeri", "quantity_centi": 100, "unit_price_ore": 1000000,
                    "rate_code": "25", "reduction_type": "rot"},          # inc 1 250 000 -> ROT 375 000
                   {"description": "Material", "quantity_centi": 100, "unit_price_ore": 200000,
                    "rate_code": "25"}],                                  # no reduction
            recipients=[{"first_name": "Anna", "last_name": "Svensson",
                         "personnummer": "811218-9876", "share_pct": 100}])
        assert inv["rut_total_ore"] == 625000
        assert inv["rot_total_ore"] == 375000
        got = ops.get_invoice(inv["invoice_id"])
        assert got["recipients"][0]["rut_amount_ore"] == 625000
        assert got["recipients"][0]["rot_amount_ore"] == 375000
        # customer pays inc - husavdrag
        assert got["customer_total_ore"] == got["inc_moms_ore"] - 1000000

    def test_recipients_required_for_reduction_lines(self, ops):
        cat, kid = self._setup(ops)
        with pytest.raises(ValueError):
            ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-01",
                               due_date="2026-03-31",
                               lines=[{"description": "Städ", "quantity_centi": 100,
                                       "unit_price_ore": 1000, "rate_code": "25",
                                       "reduction_type": "rut"}])

    def test_shares_over_100_rejected(self, ops):
        cat, kid = self._setup(ops)
        with pytest.raises(ValueError):
            ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-01",
                               due_date="2026-03-31",
                               lines=[{"description": "Städ", "quantity_centi": 100,
                                       "unit_price_ore": 1000, "rate_code": "25",
                                       "reduction_type": "rut"}],
                               recipients=[{"first_name": "A", "last_name": "B",
                                            "personnummer": "811218-9876", "share_pct": 60},
                                           {"first_name": "C", "last_name": "D",
                                            "personnummer": "19811218-9876", "share_pct": 60}])


class TestSeparateSharesAndCap:
    def _setup(self, ops):
        cat = ops.create_category("Tjänst", "income", 3001)
        kid = ops.create_customer("private", first_name="Head", last_name="H",
                                  personnummer="811218-9876")
        return cat, kid

    def test_different_rut_vs_rot_share_per_person(self, ops):
        cat, kid = self._setup(ops)
        a = ops.create_customer("private", first_name="A", last_name="A")
        b = ops.create_customer("private", first_name="B", last_name="B")
        # RUT pot 625 000, ROT pot 375 000. A takes all RUT, B takes all ROT.
        inv = ops.create_invoice(
            customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Städ", "quantity_centi": 100, "unit_price_ore": 1000000,
                    "rate_code": "25", "reduction_type": "rut"},
                   {"description": "Snickeri", "quantity_centi": 100, "unit_price_ore": 1000000,
                    "rate_code": "25", "reduction_type": "rot"}],
            recipients=[{"customer_id": a, "personnummer": "811218-9876",
                         "rut_share_pct": 100, "rot_share_pct": 0},
                        {"customer_id": b, "personnummer": "19811218-9876",
                         "rut_share_pct": 0, "rot_share_pct": 100}])
        assert inv["rut_total_ore"] == 625000 and inv["rot_total_ore"] == 375000
        recs = {r["first_name"]: r for r in ops.get_invoice(inv["invoice_id"])["recipients"]}
        assert recs["A"]["rut_amount_ore"] == 625000 and recs["A"]["rot_amount_ore"] == 0
        assert recs["B"]["rut_amount_ore"] == 0 and recs["B"]["rot_amount_ore"] == 375000
        assert recs["A"]["rut_share_pct"] == 100 and recs["A"]["rot_share_pct"] == 0

    def test_per_pot_share_validation(self, ops):
        cat, kid = self._setup(ops)
        # RUT shares sum to 120 > 100 (ROT pot is 0, so only RUT is validated)
        with pytest.raises(ValueError):
            ops.create_invoice(
                customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
                lines=[{"description": "Städ", "quantity_centi": 100, "unit_price_ore": 100000,
                        "rate_code": "25", "reduction_type": "rut"}],
                recipients=[{"first_name": "A", "last_name": "A", "personnummer": "811218-9876",
                             "rut_share_pct": 60},
                            {"first_name": "B", "last_name": "B", "personnummer": "19811218-9876",
                             "rut_share_pct": 60}])

    def test_husavdrag_cap_status_and_warning(self, ops):
        cat, kid = self._setup(ops)
        member = ops.create_customer("private", first_name="Mem", last_name="M")
        # RUT line ex 20 000 000 @25% -> inc 25 000 000 -> RUT pot 12 500 000 > 7 500 000 cap.
        inv = ops.create_invoice(
            customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Städ", "quantity_centi": 100, "unit_price_ore": 20000000,
                    "rate_code": "25", "reduction_type": "rut"}],
            recipients=[{"customer_id": member, "personnummer": "19811218-9876", "share_pct": 100}])
        status = ops.husavdrag_cap_status(member, 2026)
        assert status["used_ore"] == 12500000 and status["cap_ore"] == 7500000
        assert status["over_cap"] is True
        # the create result flagged the over-cap recipient
        assert any(w["customer_id"] == member and w["over_cap"] for w in inv["cap_warnings"])
        # a different year is unaffected
        assert ops.husavdrag_cap_status(member, 2025)["used_ore"] == 0

    def test_rot_subcap_flagged_under_combined_cap(self, ops):
        cat, kid = self._setup(ops)
        member = ops.create_customer("private", first_name="Mem", last_name="M")
        # ROT line ex 16 000 000 @25% -> inc 20 000 000 -> ROT pot 6 000 000 (60 000 kr):
        # under the combined 75 000 cap but OVER the ROT-only 50 000 sub-cap.
        inv = ops.create_invoice(
            customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Snickeri", "quantity_centi": 100, "unit_price_ore": 16000000,
                    "rate_code": "25", "reduction_type": "rot"}],
            recipients=[{"customer_id": member, "personnummer": "19811218-9876", "share_pct": 100}])
        st = ops.husavdrag_cap_status(member, 2026)
        assert st["used_ore"] == 6000000 and st["over_cap"] is False        # combined OK
        assert st["rot_used_ore"] == 6000000 and st["rot_cap_ore"] == 5000000
        assert st["rot_over_cap"] is True                                   # ROT sub-cap breached
        w = [x for x in inv["cap_warnings"] if x["customer_id"] == member][0]
        assert w["rot_over_cap"] is True and w["over_cap"] is False


class TestHouseholdRelations:
    def _two(self, ops):
        a = ops.create_customer("private", first_name="A", last_name="B")
        b = ops.create_customer("private", first_name="C", last_name="D")
        return a, b

    def test_link_list_unlink(self, ops):
        a, b = self._two(ops)
        ops.link_customers(a, b)
        ops.link_customers(b, a)                       # idempotent, order-independent
        rel_a = ops.list_related_customers(a)
        assert [r["kundnummer"] for r in rel_a] == [b]
        assert ops.list_related_customers(b)[0]["kundnummer"] == a
        ops.unlink_customers(a, b)
        assert ops.list_related_customers(a) == []

    def test_cannot_link_self(self, ops):
        a, _ = self._two(ops)
        with pytest.raises(ValueError):
            ops.link_customers(a, a)

    def test_recipient_customer_gets_pnr_and_link(self, ops):
        cat = ops.create_category("Tjänst", "income", 3001)
        head = ops.create_customer("private", first_name="Head", last_name="H",
                                   personnummer="811218-9876")
        member = ops.create_customer("private", first_name="Mem", last_name="M")  # no pnr yet
        ops.create_invoice(
            customer_id=head, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Städ", "quantity_centi": 100, "unit_price_ore": 100000,
                    "rate_code": "25", "reduction_type": "rut"}],
            recipients=[{"customer_id": member, "personnummer": "19811218-9876", "share_pct": 100}])
        assert ops.get_customer(member)["personnummer"] == "8112189876"   # saved onto customer
        assert ops.list_related_customers(head)[0]["kundnummer"] == member  # auto-linked


class TestInvoicesSnapshot:
    def _setup(self, ops):
        cat = ops.create_category("Tjänster", "income", 3001)
        kid = ops.create_customer("private", first_name="Anna", last_name="Svensson",
                                  personnummer="811218-9876", address="Storgatan 1, Stockholm")
        return cat, kid

    def test_get_invoice_decrypts_buyer_and_snapshots(self, ops):
        ops.set_company(name="Min Firma AB", org_nr="556677-8899")
        ops.create_payment_method("Swish", "123 456 78 90")
        cat, kid = self._setup(ops)
        inv = ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-01",
                                 due_date="2026-03-31",
                                 lines=[{"description": "X", "quantity_centi": 100,
                                         "unit_price_ore": 100000, "rate_code": "25"}])
        got = ops.get_invoice(inv["invoice_id"])
        assert got["buyer"]["first_name"] == "Anna"
        assert got["buyer"]["personnummer"] == "811218-9876"
        assert got["seller"]["name"] == "Min Firma AB"
        assert got["payment_methods"][0]["label"] == "Swish"
        assert got["lines"][0]["description"] == "X"


class TestLogo:
    def _png(self, fmt="PNG"):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (200, 80), (20, 90, 170)).save(buf, format=fmt)
        return buf.getvalue()

    def test_set_get_delete_logo(self, ops):
        assert ops.get_company()["has_logo"] is False
        assert ops.get_logo() is None
        ops.set_logo(self._png("PNG"))
        assert ops.get_company()["has_logo"] is True
        data, mime = ops.get_logo()
        assert mime == "image/png" and data[:4] == b"\x89PNG"
        ops.delete_logo()
        assert ops.get_company()["has_logo"] is False and ops.get_logo() is None

    def test_logo_normalises_webp_to_png(self, ops):
        ops.set_logo(self._png("WEBP"))               # uploaded as webp
        data, mime = ops.get_logo()
        assert mime == "image/png" and data[:4] == b"\x89PNG"

    def test_logo_rejects_garbage(self, ops):
        with pytest.raises(ValueError):
            ops.set_logo(b"not an image")


class TestInvoiceAddresses:
    def test_billing_shipping_and_vat_in_snapshot(self, ops):
        cat = ops.create_category("Tjänst", "income", 3001)
        kid = ops.create_customer("business", company_name="Köpare AB", org_nr="551122-3344",
                                  vat_nr="SE551122334401", address="Kungsgatan 5, Göteborg",
                                  shipping_address="Lagervägen 9, Mölndal")
        inv = ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-01",
                                 due_date="2026-03-31",
                                 lines=[{"description": "X", "quantity_centi": 100,
                                         "unit_price_ore": 100000, "rate_code": "25"}])
        buyer = ops.get_invoice(inv["invoice_id"])["buyer"]
        assert buyer["address"] == "Kungsgatan 5, Göteborg"
        assert buyer["shipping_address"] == "Lagervägen 9, Mölndal"
        assert buyer["vat_nr"] == "SE551122334401"


class TestInvoiceLifecycle:
    def _inv(self, ops, **kw):
        cat = ops.create_category("Tjänst", "income", 3001)
        kid = ops.create_customer("private", first_name="A", last_name="B",
                                  personnummer="811218-9876")
        return ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-01",
                                  due_date="2026-03-31",
                                  lines=[{"description": "X", "quantity_centi": 100,
                                          "unit_price_ore": 100000, "rate_code": "25"}], **kw)

    def test_makulera_unpaid_invoice(self, ops):
        inv = self._inv(ops)
        tid = inv["transaktion_id"]
        ops.cancel_invoice(inv["invoice_id"])
        got = ops.get_invoice(inv["invoice_id"])
        assert got["state"] == "cancelled"
        # the pending transaktion is gone (no longer payable)
        assert ops.conn.execute("SELECT 1 FROM transaktion WHERE id=?", (tid,)).fetchone() is None
        with pytest.raises(InvalidState):
            ops.cancel_invoice(inv["invoice_id"])         # already cancelled

    def test_cannot_makulera_booked_invoice(self, ops):
        inv = self._inv(ops)
        ops.register_payment(inv["transaktion_id"], "2026-03-10")
        with pytest.raises(InvalidState):
            ops.cancel_invoice(inv["invoice_id"])

    def test_kreditera_booked_invoice_reverses_ledger(self, ops):
        from backend.reports import vat as vat_report
        inv = self._inv(ops)
        ops.pay_invoice(inv["invoice_id"], date="2026-03-10")        # full payment (kontantmetod)
        before = vat_report.momsdeklaration(ops.conn, "2026-01-01", "2026-03-31")["boxes"]["10"]
        assert before == 25000
        res = ops.credit_invoice(inv["invoice_id"], reason="fel pris", date="2026-03-20")
        assert res["ver_number"] == 2                      # the credit verifikation
        got = ops.get_invoice(inv["invoice_id"])
        assert got["state"] == "credited"
        # the credit nets the moms back out in the same period
        after = vat_report.momsdeklaration(ops.conn, "2026-01-01", "2026-03-31")["boxes"]["10"]
        assert after == 0

    def test_cannot_kreditera_unpaid_kontantmetod_invoice(self, ops):
        inv = self._inv(ops)   # kontantmetod, unpaid -> nothing recognised to reverse
        with pytest.raises(InvalidState):
            ops.credit_invoice(inv["invoice_id"], reason="x")


class TestPerLineCategory:
    def _two_cat_invoice(self, ops, **kw):
        it = ops.create_category("Försäljning IT-tjänster", "income", 3001, default_rate_code="25")
        varor = ops.create_category("Försäljning varor", "income", 3002, default_rate_code="25")
        kid = ops.create_customer("business", company_name="Köpare AB", org_nr="551122-3344")
        inv = ops.create_invoice(
            customer_id=kid, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Konsulttimmar", "quantity_centi": 100,
                    "unit_price_ore": 100000, "rate_code": "25", "category_id": it},
                   {"description": "Hårdvara", "quantity_centi": 100,
                    "unit_price_ore": 40000, "rate_code": "25", "category_id": varor}], **kw)
        return inv, it, varor

    def test_booking_splits_income_across_line_categories(self, ops):
        inv, it, varor = self._two_cat_invoice(ops)
        res = ops.pay_invoice(inv["invoice_id"], date="2026-03-10")   # kontantmetod
        konton = {p["bas_konto"]: p["amount_ore"]
                  for p in _postings(ops, res["verifikation_id"])}
        assert konton[3001] == -100000      # IT-tjänster income credited
        assert konton[3002] == -40000       # varor income credited
        assert konton[2610] == -35000       # 25 % moms on 140000
        assert konton[1930] == 175000       # bank receives inc total

    def test_result_report_splits_by_line_category(self, ops):
        inv, it, varor = self._two_cat_invoice(ops)
        ops.pay_invoice(inv["invoice_id"], date="2026-03-10")
        from backend.reports import result as result_report
        rep = result_report.result_report(ops.conn, "2026-01-01", "2026-03-31")
        by = {r["bas_konto"]: r["amount_ore"] for r in rep["by_category"]}
        assert by[3001] == 100000 and by[3002] == 40000
        assert rep["income_ore"] == 140000

    def test_line_without_category_uses_invoice_default(self, ops):
        cat = ops.create_category("Tjänst", "income", 3001)
        kid = ops.create_customer("business", company_name="X AB")
        inv = ops.create_invoice(
            customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "A", "quantity_centi": 100, "unit_price_ore": 100000,
                    "rate_code": "25"}])
        got = ops.get_invoice(inv["invoice_id"])
        assert got["lines"][0]["category_id"] == cat

    def test_invoice_line_without_any_category_rejected(self, ops):
        kid = ops.create_customer("business", company_name="X AB")
        with pytest.raises(ValueError):
            ops.create_invoice(
                customer_id=kid, invoice_date="2026-03-01", due_date="2026-03-31",
                lines=[{"description": "A", "quantity_centi": 100,
                        "unit_price_ore": 100000, "rate_code": "25"}])


class TestDeleteCategory:
    def test_delete_unused_category_removes_it_and_orphan_account(self, ops):
        cid = ops.create_category("Oanvänd", "income", 3999)
        res = ops.delete_category(cid)
        assert res["deleted"] is True and res["account_removed"] is True
        assert ops.conn.execute("SELECT 1 FROM category WHERE id=?", (cid,)).fetchone() is None
        # the orphaned, never-posted, non-system konto is cleaned up too
        assert ops.conn.execute("SELECT 1 FROM account WHERE bas_konto=3999").fetchone() is None

    def test_cannot_delete_used_category(self, ops):
        cat = ops.create_category("Använd", "income", 3001)
        kid = ops.create_customer("business", company_name="X AB")
        ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-01",
                           due_date="2026-03-31",
                           lines=[{"description": "A", "quantity_centi": 100,
                                   "unit_price_ore": 100000, "rate_code": "25"}])
        assert ops.category_in_use(cat) is True
        with pytest.raises(InvalidState):
            ops.delete_category(cat)
        assert ops.conn.execute("SELECT 1 FROM category WHERE id=?", (cat,)).fetchone() is not None

    def test_delete_keeps_shared_account(self, ops):
        a = ops.create_category("A", "income", 3001)
        ops.create_category("B", "income", 3001)        # shares konto 3001
        ops.delete_category(a)
        # konto stays because another category still points at it
        assert ops.conn.execute("SELECT 1 FROM account WHERE bas_konto=3001").fetchone() is not None

    def test_delete_unknown_category_raises(self, ops):
        with pytest.raises(KeyError):
            ops.delete_category(99999)


class TestStructuredAddress:
    def test_compose_and_country_default(self, ops):
        kid = ops.create_customer("private", first_name="A", last_name="B",
                                  personnummer="811218-9876", street="Storgatan 1",
                                  zip_code="11122", city="Stockholm")
        c = ops.get_customer(kid)
        assert c["country"] == "Sverige"
        assert c["address"] == "Storgatan 1, 11122 Stockholm"   # SE country omitted

    def test_foreign_country_shown(self, ops):
        kid = ops.create_customer("business", company_name="Oy AB", street="Mannerheim 1",
                                  zip_code="00100", city="Helsinki", country="Finland")
        assert ops.get_customer(kid)["address"].endswith("Finland")

    def test_update_recomposes_address(self, ops):
        kid = ops.create_customer("private", first_name="A", last_name="B",
                                  personnummer="811218-9876", street="Gata 1",
                                  zip_code="111", city="Ort")
        ops.update_customer(kid, city="Annan ort")
        assert "Annan ort" in ops.get_customer(kid)["address"]


class TestCategoryDefaultRate:
    def test_create_and_list_default_rate(self, ops):
        ops.create_category("IT 25%", "income", 3001, default_rate_code="25")
        row = ops.conn.execute(
            "SELECT default_rate_code FROM category WHERE bas_konto=3001").fetchone()
        assert row["default_rate_code"] == "25"

    def test_invalid_default_rate_rejected(self, ops):
        with pytest.raises(ValueError):
            ops.create_category("Bad", "income", 3001, default_rate_code="99")

    def test_system_accounts_listed(self, ops):
        sys = ops.system_accounts()
        assert sys[1930] and sys[2610]          # bank + utgående moms 25 % labelled


class TestFakturametod:
    def _setup(self, ops):
        cat = ops.create_category("Tjänst", "income", 3001)
        kid = ops.create_customer("private", first_name="A", last_name="B",
                                  personnummer="811218-9876")
        return cat, kid

    def _postings(self, ops, vid):
        return {r["bas_konto"]: r["amount_ore"] for r in ops.conn.execute(
            "SELECT bas_konto, amount_ore FROM posting WHERE verifikation_id=?", (vid,))}

    def test_default_is_kontantmetod(self, ops):
        assert ops.get_accounting_method() == "kontantmetod"
        cat, kid = self._setup(ops)
        inv = ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-01",
                                 due_date="2026-03-31",
                                 lines=[{"description": "X", "quantity_centi": 100,
                                         "unit_price_ore": 100000, "rate_code": "25"}])
        # kontantmetod: nothing booked at issue
        v = ops.conn.execute("SELECT verifikation_id FROM transaktion WHERE id=?",
                             (inv["transaktion_id"],)).fetchone()["verifikation_id"]
        assert v is None

    def test_fakturametod_books_at_issue_and_payment(self, ops):
        from backend.reports import vat as vat_report
        ops.set_accounting_method("fakturametod")
        cat, kid = self._setup(ops)
        inv = ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-01",
                                 due_date="2026-03-31",
                                 lines=[{"description": "X", "quantity_centi": 100,
                                         "unit_price_ore": 100000, "rate_code": "25"}])
        tid = inv["transaktion_id"]
        # issue verifikation: kundfordran 1510 / income 3001 / moms 2610
        vid = ops.conn.execute("SELECT verifikation_id FROM transaktion WHERE id=?",
                               (tid,)).fetchone()["verifikation_id"]
        assert vid is not None
        p = self._postings(ops, vid)
        assert p[1510] == 125000 and p[3001] == -100000 and p[2610] == -25000
        # moms reported in the invoice's period already
        assert vat_report.momsdeklaration(ops.conn, "2026-01-01", "2026-03-31")["boxes"]["10"] == 25000
        assert ops.get_invoice(inv["invoice_id"])["state"] == "pending"   # booked but unpaid

        # payment (later quarter) settles the receivable: bank / kundfordran, no moms
        res = ops.register_payment(tid, "2026-05-10")
        pay = self._postings(ops, res["verifikation_id"])
        assert pay[1930] == 125000 and pay[1510] == -125000
        # moms stays in Q1, not the payment quarter
        assert vat_report.momsdeklaration(ops.conn, "2026-04-01", "2026-06-30")["boxes"]["10"] == 0
        assert ops.get_invoice(inv["invoice_id"])["state"] == "paid"

    def test_fakturametod_rut_split_at_issue(self, ops):
        ops.set_accounting_method("fakturametod")
        cat, kid = self._setup(ops)
        inv = ops.create_invoice(
            customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Städ", "quantity_centi": 100, "unit_price_ore": 1000000,
                    "rate_code": "25", "reduction_type": "rut"}],
            recipients=[{"first_name": "A", "last_name": "B", "personnummer": "811218-9876",
                         "share_pct": 100}])
        vid = ops.conn.execute("SELECT verifikation_id FROM transaktion WHERE id=?",
                               (inv["transaktion_id"],)).fetchone()["verifikation_id"]
        p = self._postings(ops, vid)
        # inc 1 250 000; RUT pot 625 000; customer owes inc - rut = 625 000 on 1510, 625 000 on 1513
        assert p[1510] == 625000 and p[1513] == 625000


class TestPartialSettlement:
    def _inv(self, ops, amount_ore=100003):
        cat = ops.create_category("T", "income", 3001)
        kid = ops.create_customer("private", first_name="A", last_name="B",
                                  personnummer="811218-9876")
        # ex 100003 @ 25% -> moms 25001, inc 125004 (deliberately awkward for rounding)
        return ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-02-01",
                                  due_date="2026-02-28",
                                  lines=[{"description": "X", "quantity_centi": 100,
                                          "unit_price_ore": amount_ore, "rate_code": "25"}])

    def test_kontant_partials_reconcile_to_ore(self, ops):
        from backend.reports import vat as vat_report
        inv = self._inv(ops)                                   # inc 125004
        for amt, d in [(33333, "2026-02-05"), (50000, "2026-02-10")]:
            ops.pay_invoice(inv["invoice_id"], amt, d)
        ops.pay_invoice(inv["invoice_id"], date="2026-02-20")  # remainder
        b = ops.get_invoice(inv["invoice_id"])
        assert b["outstanding_ore"] == 0 and b["state"] == "paid"
        # the three proportional slices reconcile to the exact full moms (25001)
        assert vat_report.momsdeklaration(ops.conn, "2026-01-01", "2026-02-28")["boxes"]["10"] == 25001

    def test_fakturametod_partials_settle_receivable(self, ops):
        ops.set_accounting_method("fakturametod")
        inv = self._inv(ops)                                   # inc 125004
        ops.pay_invoice(inv["invoice_id"], 25004, "2026-02-05")
        assert ops.get_invoice(inv["invoice_id"])["outstanding_ore"] == 100000
        ops.pay_invoice(inv["invoice_id"], date="2026-02-20")
        b = ops.get_invoice(inv["invoice_id"])
        assert b["outstanding_ore"] == 0 and b["state"] == "paid"
        for v in ops.conn.execute("SELECT id FROM verifikation"):
            assert sum(p["amount_ore"] for p in ops.conn.execute(
                "SELECT amount_ore FROM posting WHERE verifikation_id=?", (v["id"],))) == 0


class TestOresavrundning:
    """Öresavrundning (avrundningslagen): the customer's summa att betala is rounded to
    whole kronor, but per Skatteverket the beskattningsunderlag and moms are NEVER
    rounded — the öre difference goes to 3740 Öres- och kronutjämning."""

    def _net(self, ops):
        return {r["bas_konto"]: r["s"] for r in ops.conn.execute(
            "SELECT bas_konto, SUM(amount_ore) s FROM posting GROUP BY bas_konto")}

    def _cust(self, ops):
        return ops.create_customer("business", company_name="Kund AB")

    def test_round_helper_direction(self):
        from backend.db.operations import _round_to_krona
        assert _round_to_krona(0) == 0
        assert _round_to_krona(31) == 0 and _round_to_krona(49) == 0        # 1-49 down
        assert _round_to_krona(50) == 100 and _round_to_krona(99) == 100    # 50-99 up
        assert _round_to_krona(558469) == 558500 and _round_to_krona(558431) == 558400
        assert _round_to_krona(-31) == 0 and _round_to_krona(-70) == -100

    def test_fakturametod_matches_skatteverket_example(self, ops):
        # SKV example: 527 kr ex, 25 % moms 131,75 -> inc 658,75 -> öresutjämnas till 659.
        ops.set_accounting_method("fakturametod")
        cat = ops.create_category("Försäljning", "income", 3001, default_rate_code="25")
        inv = ops.create_invoice(customer_id=self._cust(ops), category_id=cat,
            invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Vara", "quantity_centi": 100, "unit_price_ore": 52700,
                    "rate_code": "25"}])
        assert ops.get_invoice(inv["invoice_id"])["inc_moms_ore"] == 65875
        ops.pay_invoice(inv["invoice_id"], date="2026-03-15")
        net = self._net(ops)
        assert net[3001] == -52700              # beskattningsunderlag EXACT
        assert net[2610] == -13175              # moms EXACT (131,75)
        assert net[1930] == 65900               # bank = 659 kr (rounded)
        assert net[3740] == -25                 # öresutjämning 0,25 kr kredit
        assert net[1510] == 0                   # kundfordran cleared
        for v in ops.conn.execute("SELECT id FROM verifikation"):
            assert sum(p["amount_ore"] for p in ops.conn.execute(
                "SELECT amount_ore FROM posting WHERE verifikation_id=?", (v["id"],))) == 0

    def test_kontantmetod_rounds_at_payment(self, ops):
        # inc 5584,69 (Anna) -> att betala 5585,00; base/moms exact, öre -> 3740.
        cat = ops.create_category("Trädgård", "income", 3001, default_rate_code="25")
        inv = ops.create_invoice(customer_id=self._cust(ops), category_id=cat,
            invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Växter", "quantity_centi": 100, "unit_price_ore": 350000, "rate_code": "25"},
                   {"description": "Plantering", "quantity_centi": 100, "unit_price_ore": 72000, "rate_code": "25"},
                   {"description": "Mil", "quantity_centi": 100, "unit_price_ore": 24775, "rate_code": "25"}])
        assert ops.get_invoice(inv["invoice_id"])["inc_moms_ore"] == 558469
        ops.pay_invoice(inv["invoice_id"], date="2026-03-15")
        net = self._net(ops)
        assert net[3001] == -446775 and net[2610] == -111694    # underlag + moms EXACT
        assert net[1930] == 558500                              # bank = 5585,00
        assert net[3740] == 558469 - 558500                     # öresutjämning +0,31 debet

    def test_rut_customer_part_rounded_husavdrag_exact(self, ops):
        cat = ops.create_category("Städ", "income", 3011, default_rate_code="25")
        kid = ops.create_customer("private", first_name="Anna", last_name="S",
                                  personnummer="811218-9876")
        inv = ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-01",
            due_date="2026-03-31",
            lines=[{"description": "Städ", "quantity_centi": 100, "unit_price_ore": 446775,
                    "rate_code": "25", "reduction_type": "rut"}],
            recipients=[{"first_name": "Anna", "last_name": "S", "personnummer": "811218-9876",
                         "share_pct": 100}])
        full = ops.get_invoice(inv["invoice_id"])
        rut = full["rut_total_ore"]; cust_exact = full["inc_moms_ore"] - rut
        r = ops.register_payment(full["transaktion_id"], "2026-03-10")
        p = {row["bas_konto"]: row["amount_ore"] for row in ops.conn.execute(
            "SELECT bas_konto, amount_ore FROM posting WHERE verifikation_id=?", (r["verifikation_id"],))}
        from backend.db.operations import _round_to_krona
        assert p[1930] == _round_to_krona(cust_exact)           # kundens del rounded
        assert p[1513] == rut                                   # Skatteverkets del EXACT
        assert p[3011] == -446775 and p[2610] == -111694        # underlag + moms EXACT
        assert p[3740] == cust_exact - _round_to_krona(cust_exact)
        assert sum(p.values()) == 0

    def test_whole_krona_invoice_has_no_ores_posting(self, ops):
        cat = ops.create_category("T", "income", 3001, default_rate_code="25")
        # ex 100000 @ 25 % -> inc 125000 (already whole kronor)
        inv = ops.create_invoice(customer_id=self._cust(ops), category_id=cat,
            invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "X", "quantity_centi": 100, "unit_price_ore": 100000, "rate_code": "25"}])
        ops.pay_invoice(inv["invoice_id"], date="2026-03-15")
        assert 3740 not in self._net(ops)                       # no öresavrundning needed


# ---------------------------------------------------------------------------
# Category prefixes + category-driven article numbering
# ---------------------------------------------------------------------------

class TestCategoryPrefix:
    def test_auto_prefix_is_lowest_unused(self, ops):
        c1 = ops.create_category("A", "income", 3001)
        c2 = ops.create_category("B", "income", 3002)
        p1 = ops.conn.execute("SELECT prefix FROM category WHERE id=?", (c1,)).fetchone()[0]
        p2 = ops.conn.execute("SELECT prefix FROM category WHERE id=?", (c2,)).fetchone()[0]
        assert p1 == "0000" and p2 == "0001"

    def test_explicit_prefix_and_duplicate_rejected(self, ops):
        ops.create_category("A", "income", 3001, prefix="0500")
        assert ops.prefix_in_use("0500") is True
        with pytest.raises(InvalidState):
            ops.create_category("B", "income", 3002, prefix="0500")

    def test_bad_prefix_rejected(self, ops):
        with pytest.raises(ValueError):
            ops.create_category("A", "income", 3001, prefix="12")

    def test_article_number_uses_category_prefix(self, ops):
        cid = ops.create_category("Nätverk", "income", 3001, prefix="0007")
        art = ops.create_article("Router", category_id=cid)
        assert art["article_number"].startswith("0007-")

    def test_uncategorised_article_gets_NY_then_renumbers_on_categorise(self, ops):
        art = ops.create_article("Lös pryl")               # no category
        assert art["article_number"].startswith("NY-")
        cid = ops.create_category("Prylar", "income", 3001, prefix="0042")
        ops.update_article(art["id"], category_id=cid)
        num = ops.conn.execute("SELECT article_number FROM article WHERE id=?",
                               (art["id"],)).fetchone()[0]
        assert num.startswith("0042-")

    def test_invoiced_article_number_frozen_on_recategorise(self, ops):
        c_sell = ops.create_category("Tjänst", "income", 3001, prefix="0001")
        kid = ops.create_customer("business", company_name="X AB", org_nr="556000-0001")
        art = ops.create_article("Vara", category_id=c_sell)
        ops.create_invoice(customer_id=kid, category_id=c_sell, invoice_date="2026-03-01",
            due_date="2026-03-31",
            lines=[{"description": "Vara", "quantity_centi": 100, "unit_price_ore": 10000,
                    "rate_code": "25", "category_id": c_sell, "article_id": art["id"]}])
        before = ops.conn.execute("SELECT article_number FROM article WHERE id=?",
                                  (art["id"],)).fetchone()[0]
        c2 = ops.create_category("Annat", "income", 3002, prefix="0099")
        ops.update_article(art["id"], category_id=c2)       # used -> number frozen
        after = ops.conn.execute("SELECT article_number FROM article WHERE id=?",
                                 (art["id"],)).fetchone()[0]
        assert after == before

    def test_find_or_create_reuses_same_article(self, ops):
        cid = ops.create_category("Nät", "income", 3001, prefix="0003")
        a = ops.find_or_create_article("Kabel", cid)
        b = ops.find_or_create_article(" kabel ", cid)      # trimmed + case-insensitive
        assert a == b


class TestSubcategories:
    def test_subcategory_inherits_parent(self, ops):
        parent = ops.create_category("Hårdvara", "income", 3010, default_rate_code="25")
        sub = ops.create_category("Nätverk", "income", parent_id=parent)   # no konto/kind
        row = ops.conn.execute(
            "SELECT kind, bas_konto, default_rate_code, parent_id FROM category WHERE id=?",
            (sub,)).fetchone()
        assert row["parent_id"] == parent
        assert row["kind"] == "income"
        assert row["bas_konto"] == 3010          # inherited
        assert row["default_rate_code"] == "25"  # inherited

    def test_subcategory_own_konto_allowed(self, ops):
        parent = ops.create_category("Hårdvara", "income", 3010)
        sub = ops.create_category("Nätverk", "income", 3011, parent_id=parent)
        assert ops.conn.execute("SELECT bas_konto FROM category WHERE id=?",
                                (sub,)).fetchone()[0] == 3011

    def test_descendants_and_nesting(self, ops):
        a = ops.create_category("A", "income", 3001)
        b = ops.create_category("B", "income", parent_id=a)
        c = ops.create_category("C", "income", parent_id=b)   # deep nesting
        assert ops.category_descendants(a) == {b, c}
        assert ops.category_descendants(b) == {c}

    def test_reparent_cycle_refused(self, ops):
        a = ops.create_category("A", "income", 3001)
        b = ops.create_category("B", "income", parent_id=a)
        with pytest.raises(InvalidState):
            ops.update_category(a, parent_id=b)      # a under its own descendant -> cycle
        with pytest.raises(InvalidState):
            ops.update_category(a, parent_id=a)      # under itself

    def test_reparent_to_top_level(self, ops):
        a = ops.create_category("A", "income", 3001)
        b = ops.create_category("B", "income", parent_id=a)
        ops.update_category(b, parent_id=0)          # 0 -> make top-level
        assert ops.conn.execute("SELECT parent_id FROM category WHERE id=?",
                                (b,)).fetchone()[0] is None


# ---------------------------------------------------------------------------
# Stock / lager (inventory batches + real margin)
# ---------------------------------------------------------------------------

class TestStock:
    def _art(self, ops):
        return ops.create_article("Router", "1000", unit_price_ore=100000)["id"]

    def _cust(self, ops):
        ops.create_category("Försäljning", "income", 3001)  # ensure a category exists
        return ops.create_customer("business", company_name="Acme AB", org_nr="556000-0001")

    def test_add_batch_assigns_sequential_number(self, ops):
        aid = self._art(ops)
        b1 = ops.add_stock_batch(aid, 500, 60000, received_date="2026-03-01")
        b2 = ops.add_stock_batch(aid, 300, 65000, received_date="2026-04-01")
        assert b1["batch_number"] == 1 and b2["batch_number"] == 2
        batches = ops.list_article_batches(aid)
        assert [b["batch_number"] for b in batches] == [2, 1]      # newest first
        assert batches[1]["qty_remaining_centi"] == 500

    def test_batch_number_is_per_article(self, ops):
        a1 = ops.create_article("Router", "1000")["id"]
        a2 = ops.create_article("Switch", "1000")["id"]
        ops.add_stock_batch(a1, 100, 500)
        ops.add_stock_batch(a2, 100, 700)              # separate article -> also starts at 1
        b1 = ops.add_stock_batch(a1, 100, 550)
        assert ops.list_article_batches(a2)[0]["batch_number"] == 1
        assert b1["batch_number"] == 2                 # a1's second batch
        # full batch id = article_number + batch_number
        full = ops.list_article_batches(a2)[0]["full_batch_id"]
        assert full.endswith("-1")

    def test_list_stock_summary(self, ops):
        aid = self._art(ops)
        ops.add_stock_batch(aid, 500, 60000)   # 5 * 600 = 3000 kr
        ops.add_stock_batch(aid, 300, 65000)   # 3 * 650 = 1950 kr
        stock = ops.list_stock()
        assert len(stock) == 1
        row = stock[0]
        assert row["qty_remaining_centi"] == 800
        assert row["batch_count"] == 2
        assert row["value_ore"] == 60000 * 5 + 65000 * 3   # 495000

    def test_invoice_line_consumes_batch_and_freezes_margin(self, ops):
        cat = ops.create_category("Försäljning", "income", 3001)
        kid = ops.create_customer("business", company_name="Acme AB", org_nr="556000-0001")
        aid = ops.create_article("Router", "1000")["id"]
        batch = ops.add_stock_batch(aid, 500, 60000)   # 5 st @ 600 kr cost
        inv = ops.create_invoice(
            customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Router", "quantity_centi": 200, "unit_price_ore": 100000,
                    "rate_code": "25", "article_id": aid, "stock_batch_id": batch["id"]}])
        # 2 st sold: cost 2*600 = 1200 kr; revenue ex 2000 kr -> margin 800 kr
        got = ops.get_invoice(inv["invoice_id"])
        assert got["cost_ore"] == 120000
        assert got["margin_ore"] == 200000 - 120000
        assert got["has_cost"] is True
        assert got["lines"][0]["batch_number"] == 1
        # batch consumed: 5 - 2 = 3 remaining
        assert ops.list_article_batches(aid)[0]["qty_remaining_centi"] == 300
        # list_invoices carries the same margin
        summ = [i for i in ops.list_invoices() if i["id"] == inv["invoice_id"]][0]
        assert summ["cost_ore"] == 120000 and summ["margin_ore"] == 80000

    def test_invoice_without_batch_has_unknown_cost(self, ops):
        cat = ops.create_category("Försäljning", "income", 3001)
        kid = ops.create_customer("business", company_name="Acme AB", org_nr="556000-0001")
        inv = ops.create_invoice(
            customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Konsult", "quantity_centi": 100, "unit_price_ore": 100000,
                    "rate_code": "25"}])
        got = ops.get_invoice(inv["invoice_id"])
        assert got["cost_ore"] == 0 and got["has_cost"] is False

    def test_overconsumption_refused(self, ops):
        cat = ops.create_category("Försäljning", "income", 3001)
        kid = ops.create_customer("business", company_name="Acme AB", org_nr="556000-0001")
        aid = ops.create_article("Router", "1000")["id"]
        batch = ops.add_stock_batch(aid, 100, 60000)   # only 1 in stock
        with pytest.raises(InvalidState):
            ops.create_invoice(
                customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
                lines=[{"description": "Router", "quantity_centi": 200, "unit_price_ore": 100000,
                        "rate_code": "25", "stock_batch_id": batch["id"]}])

    def test_makulera_restocks_batch(self, ops):
        cat = ops.create_category("Försäljning", "income", 3001)
        kid = ops.create_customer("business", company_name="Acme AB", org_nr="556000-0001")
        aid = ops.create_article("Router", "1000")["id"]
        batch = ops.add_stock_batch(aid, 500, 60000)
        inv = ops.create_invoice(
            customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Router", "quantity_centi": 200, "unit_price_ore": 100000,
                    "rate_code": "25", "stock_batch_id": batch["id"]}])
        assert ops.list_article_batches(aid)[0]["qty_remaining_centi"] == 300
        ops.cancel_invoice(inv["invoice_id"])       # makulera -> goods return
        assert ops.list_article_batches(aid)[0]["qty_remaining_centi"] == 500

    def test_delete_batch_only_when_untouched(self, ops):
        cat = ops.create_category("Försäljning", "income", 3001)
        kid = ops.create_customer("business", company_name="Acme AB", org_nr="556000-0001")
        aid = ops.create_article("Router", "1000")["id"]
        b1 = ops.add_stock_batch(aid, 500, 60000)
        b2 = ops.add_stock_batch(aid, 500, 60000)
        ops.create_invoice(
            customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Router", "quantity_centi": 100, "unit_price_ore": 100000,
                    "rate_code": "25", "stock_batch_id": b2["id"]}])
        with pytest.raises(InvalidState):
            ops.delete_stock_batch(b2["id"])         # partially consumed
        ops.delete_stock_batch(b1["id"])             # untouched -> ok
        assert all(b["id"] != b1["id"] for b in ops.list_article_batches(aid))


# ---------------------------------------------------------------------------
# Delete customer from the register (keep invoices)
# ---------------------------------------------------------------------------

class TestCustomerDelete:
    def test_delete_keeps_invoice_detaches_link(self, ops):
        cat = ops.create_category("Tjänst", "income", 3001)
        kid = ops.create_customer("business", company_name="Acme AB", org_nr="556000-0001")
        inv = ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-01",
            due_date="2026-03-31",
            lines=[{"description": "X", "quantity_centi": 100, "unit_price_ore": 10000, "rate_code": "25"}])
        ops.delete_customer(kid)
        # customer gone
        with pytest.raises(KeyError):
            ops.get_customer(kid)
        # invoice kept, link detached, frozen buyer snapshot intact
        got = ops.get_invoice(inv["invoice_id"])
        assert got["customer_id"] is None
        assert got["buyer"]["company_name"] == "Acme AB"

    def test_delete_customer_with_rut_detaches_claim(self, ops):
        cat = ops.create_category("Städ", "income", 3001)
        kid = ops.create_customer("private", first_name="Anna", last_name="A",
                                  personnummer="811218-9876")
        res = ops.record_income(kid, cat, [{"rate_code": "25", "amount_ore": 100000}],
                                "2026-03-01", rut_amount_ore=25000)
        claim_id = res["rut_claim_id"]
        ops.delete_customer(kid)
        # the RUT claim record survives (amount/state kept), only customer_id detached
        row = ops.conn.execute(
            "SELECT customer_id, rut_amount_ore FROM rut_claim WHERE id=?", (claim_id,)).fetchone()
        assert row is not None and row["customer_id"] is None
        assert row["rut_amount_ore"] == 25000

    def test_delete_removes_relations_and_support(self, ops):
        a = ops.create_customer("private", first_name="A", last_name="A", personnummer="811218-9876")
        b = ops.create_customer("private", first_name="B", last_name="B", personnummer="670919-9530")
        ops.link_customers(a, b)
        ops.record_support_entry(a, 15, "addition", "bonus")
        ops.delete_customer(a)
        assert ops.conn.execute("SELECT COUNT(*) FROM customer_relation").fetchone()[0] == 0
        assert ops.conn.execute("SELECT COUNT(*) FROM support_ledger WHERE customer_id=?",
                                (a,)).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Inköp extras: öresavrundning + edit non-ledger fields
# ---------------------------------------------------------------------------

class TestExpenseExtras:
    def test_ores_rounding_books_diff_to_3740(self, ops):
        cat = ops.create_category("Inköp varor", "expense", 4010)
        res = ops.record_expense(None, cat,
            [{"rate_code": "25", "amount_ore": 12499, "inclusive": True}],
            "2026-05-01", ores_rounding=True, paid_date="2026-05-01")
        vid = res["verifikation_id"]
        postings = {p["bas_konto"]: p["amount_ore"] for p in _postings(ops, vid)}
        assert postings[1930] == -12500      # bank pays whole kronor
        assert postings[3740] == 1           # öre diff to öres-/kronutjämning
        assert postings[4010] == 9999        # ex-moms exact
        assert postings[2640] == 2500        # ingående moms exact
        assert _balance(ops, vid) == 0

    def test_no_rounding_by_default(self, ops):
        cat = ops.create_category("Inköp varor", "expense", 4010)
        res = ops.record_expense(None, cat,
            [{"rate_code": "25", "amount_ore": 12499, "inclusive": True}],
            "2026-05-01", paid_date="2026-05-01")
        postings = {p["bas_konto"]: p["amount_ore"] for p in _postings(ops, res["verifikation_id"])}
        assert postings[1930] == -12499 and 3740 not in postings

    def test_update_expense_meta_keeps_ledger(self, ops):
        cat = ops.create_category("Inköp varor", "expense", 4010)
        sup = ops.create_supplier("Inet")
        res = ops.record_expense(None, cat,
            [{"rate_code": "25", "amount_ore": 10000, "inclusive": False}],
            "2026-05-01", ext_ref="K1", paid_date="2026-05-01")
        tid = res["transaktion_id"]
        ops.update_expense_meta(tid, supplier_id=sup, ext_ref="K2", note="rättat",
                                receipt_original_format="digital")
        row = ops.conn.execute("SELECT supplier_id, ext_ref, note, category_id, "
            "receipt_original_format FROM transaktion WHERE id=?", (tid,)).fetchone()
        assert row["supplier_id"] == sup and row["ext_ref"] == "K2"
        assert row["note"] == "rättat" and row["receipt_original_format"] == "digital"
        assert row["category_id"] == cat        # BAS-konto untouched
        # moms/belopp untouched
        assert ops._sum_moms(tid)[0] == 10000

    def test_update_expense_meta_refuses_sale(self, ops):
        icat = ops.create_category("Sälj", "income", 3001)
        kid = ops.create_customer("business", company_name="X AB")
        inc = ops.record_income(kid, icat, [{"rate_code": "25", "amount_ore": 10000}], "2026-05-01")
        with pytest.raises(InvalidState):
            ops.update_expense_meta(inc["transaktion_id"], ext_ref="Z")


class TestExpenseDrafts:
    def test_save_list_get_delete_roundtrip(self, ops):
        sup = ops.create_supplier("Inet")
        payload = {"supplier_id": sup, "category_id": None, "trans_date": "2026-05-01",
                   "items": [{"description": "Router", "quantity_centi": 200,
                              "unit_cost_ore": 60000, "rate_code": "25"}]}
        d = ops.save_expense_draft(payload)
        lst = ops.list_expense_drafts()
        assert len(lst) == 1 and lst[0]["supplier_id"] == sup and lst[0]["line_count"] == 1
        assert lst[0]["total_ore"] == 150000            # 2 × 600 × 1.25
        got = ops.get_expense_draft(d["id"])
        assert got["payload"]["items"][0]["description"] == "Router"
        # update in place
        payload["items"].append({"description": "Kabel", "quantity_centi": 100,
                                 "unit_cost_ore": 5000, "rate_code": "25"})
        ops.save_expense_draft(payload, draft_id=d["id"])
        assert ops.list_expense_drafts()[0]["line_count"] == 2
        ops.delete_expense_draft(d["id"])
        assert ops.list_expense_drafts() == []

    def test_payload_is_encrypted_at_rest(self, ops):
        d = ops.save_expense_draft({"items": [{"description": "HemligPryl"}]})
        raw = ops.conn.execute("SELECT payload_enc FROM expense_draft WHERE id=?",
                               (d["id"],)).fetchone()[0]
        assert "HemligPryl" not in raw
