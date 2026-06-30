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
