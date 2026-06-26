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
        inv = ops.create_invoice(
            customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
            lines=[{"description": "Städning", "quantity_centi": 100, "unit_price_ore": 1000000,
                    "rate_code": "25", "rut_eligible": True}],
            recipients=[{"first_name": "Anna", "last_name": "Svensson",
                         "personnummer": "811218-9876", "rut_amount_ore": 150000},
                        {"first_name": "Björn", "last_name": "Svensson",
                         "personnummer": "19811218-9876", "rut_amount_ore": 100000}])
        assert inv["rut_total_ore"] == 250000
        got = ops.get_invoice(inv["invoice_id"])
        assert len(got["recipients"]) == 2
        assert got["recipients"][0]["personnummer"] == "8112189876"   # normalized + decrypted
        assert sum(r["rut_amount_ore"] for r in got["recipients"]) == 250000

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
