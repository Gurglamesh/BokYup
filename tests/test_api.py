"""
Tests for Layer 7: the FastAPI HTTP layer.

Drives the API end-to-end with Starlette's TestClient, covering the unlock/lock
lifecycle, auto-lock, reference + bookkeeping endpoints, reports, and error mapping.
"""

from __future__ import annotations

import time
import pytest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.api import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app(tmp_path: Path):
    return create_app(app_dir=tmp_path / "app", autolock_seconds=900)


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def book(client, tmp_path):
    """Create an (unlocked) book; return its id."""
    resp = client.post("/books", json={
        "display_name": "Test AB",
        "db_path": str(tmp_path / "test.db"),
        "passphrase": "correct-horse",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Meta / books lifecycle
# ---------------------------------------------------------------------------

class TestBooksLifecycle:
    def test_root(self, client):
        assert client.get("/").json()["name"] == "BokYup API"

    def test_create_and_list(self, client, book):
        books = client.get("/books").json()
        assert len(books) == 1
        assert books[0]["display_name"] == "Test AB"

    def test_lock_then_operation_returns_423(self, client, book):
        client.post(f"/books/{book}/lock")
        resp = client.get(f"/books/{book}/categories")
        assert resp.status_code == 423

    def test_unlock_wrong_passphrase_401(self, client, book):
        client.post(f"/books/{book}/lock")
        resp = client.post(f"/books/{book}/unlock", json={"passphrase": "nope"})
        assert resp.status_code == 401

    def test_unlock_correct_passphrase(self, client, book):
        client.post(f"/books/{book}/lock")
        resp = client.post(f"/books/{book}/unlock", json={"passphrase": "correct-horse"})
        assert resp.status_code == 200
        assert client.get(f"/books/{book}/categories").status_code == 200

    def test_rename(self, client, book):
        client.patch(f"/books/{book}", json={"display_name": "Renamed AB"})
        names = [b["display_name"] for b in client.get("/books").json()]
        assert "Renamed AB" in names

    def test_remove_keeps_files_by_default(self, client, tmp_path):
        db = tmp_path / "keep.db"
        bid = client.post("/books", json={"display_name": "Keep", "db_path": str(db),
                                          "passphrase": "pw"}).json()["id"]
        resp = client.delete(f"/books/{bid}")
        assert resp.status_code == 200 and resp.json()["files_deleted"] is False
        assert client.get("/books").json() == [] or all(
            b["id"] != bid for b in client.get("/books").json())
        assert db.exists() and (tmp_path / "keep.db.key").exists()   # files untouched

    def test_remove_with_delete_files_purges(self, client, tmp_path):
        from pathlib import Path
        db = tmp_path / "purge.db"
        bid = client.post("/books", json={"display_name": "Purge", "db_path": str(db),
                                          "passphrase": "pw"}).json()["id"]
        resp = client.delete(f"/books/{bid}?delete_files=true")
        assert resp.status_code == 200 and resp.json()["files_deleted"] is True
        assert not db.exists() and not Path(str(db) + ".key").exists()
        assert all(b["id"] != bid for b in client.get("/books").json())


# ---------------------------------------------------------------------------
# Auto-lock
# ---------------------------------------------------------------------------

class TestAutoLock:
    def test_idle_session_auto_locks(self, app, tmp_path):
        with TestClient(app) as client:
            bid = client.post("/books", json={
                "display_name": "B", "db_path": str(tmp_path / "b.db"),
                "passphrase": "pw",
            }).json()["id"]
            # Simulate inactivity beyond the timeout.
            app.state.facade.autolock_seconds = 1
            app.state.facade.last_activity[bid] = time.monotonic() - 10
            resp = client.get(f"/books/{bid}/categories")
            assert resp.status_code == 423
            assert "auto-locked" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

class TestReference:
    def test_category_crud(self, client, book):
        cid = client.post(f"/books/{book}/categories",
                          json={"name": "Kontorsmaterial", "kind": "expense",
                                "bas_konto": 5460}).json()["id"]
        cats = client.get(f"/books/{book}/categories").json()
        assert any(c["id"] == cid for c in cats)

    def test_customer_personnummer_validation_400(self, client, book):
        resp = client.post(f"/books/{book}/customers",
                           json={"type": "private", "first_name": "Anna",
                                 "personnummer": "811218-9875"})  # bad Luhn
        assert resp.status_code == 400

    def test_customer_roundtrip_decrypts(self, client, book):
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "private", "first_name": "Anna",
                                "personnummer": "811218-9876"}).json()["kundnummer"]
        got = client.get(f"/books/{book}/customers/{kid}").json()
        assert got["personnummer"] == "811218-9876"

    def test_customer_list_exposes_invoiced_total(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "T", "kind": "income", "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "Alfa AB"}).json()["kundnummer"]
        assert client.get(f"/books/{book}/customers").json()[0]["invoiced_ore"] == 0
        for _ in range(2):
            client.post(f"/books/{book}/invoices", json={
                "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
                "due_date": "2026-03-31",
                "lines": [{"description": "J", "quantity_centi": 100, "unit_price_ore": 100000,
                           "rate_code": "25", "category_id": cat}]})
        assert client.get(f"/books/{book}/customers").json()[0]["invoiced_ore"] == 250000


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------

class TestBookkeeping:
    def _setup_income(self, client, book, paid=True):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "Försäljning", "kind": "income",
                                "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "ACME AB"}).json()["kundnummer"]
        body = {"customer_id": kid, "category_id": cat,
                "lines": [{"rate_code": "25", "amount_ore": 1250}],
                "trans_date": "2026-02-10"}
        if paid:
            body["paid_date"] = "2026-02-10"
        return client.post(f"/books/{book}/incomes", json=body)

    def test_income_books_verifikation(self, client, book):
        resp = self._setup_income(client, book)
        assert resp.status_code == 201
        assert resp.json()["ver_number"] == 1

    def test_pending_then_pay(self, client, book):
        res = self._setup_income(client, book, paid=False).json()
        tid = res["transaktion_id"]
        assert "verifikation_id" not in res
        pay = client.post(f"/books/{book}/transaktioner/{tid}/pay",
                          json={"payment_date": "2026-02-15"})
        assert pay.status_code == 200
        assert pay.json()["ver_number"] == 1

    def test_period_lock_blocks_booking_409(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "X", "kind": "expense", "bas_konto": 5460}).json()["id"]
        client.post(f"/books/{book}/period-locks",
                    json={"period_start": "2026-01-01", "period_end": "2026-03-31"})
        resp = client.post(f"/books/{book}/expenses",
                           json={"category_id": cat,
                                 "lines": [{"rate_code": "25", "amount_ore": 100}],
                                 "trans_date": "2026-02-01", "paid_date": "2026-02-01"})
        assert resp.status_code == 409

    def test_purchase_with_ext_ref_and_pay_later(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "Förbrukning", "kind": "expense", "bas_konto": 5460}).json()["id"]
        sup = client.post(f"/books/{book}/suppliers",
                          json={"name": "Inet", "default_moms_rate": "25"}).json()["id"]
        # an incoming supplier invoice (not paid yet) with a kvitto-/fakturanummer
        res = client.post(f"/books/{book}/expenses",
                          json={"supplier_id": sup, "category_id": cat, "ext_ref": "FAKT-2211",
                                "lines": [{"rate_code": "25", "amount_ore": 125000, "inclusive": True}],
                                "trans_date": "2026-03-01"}).json()
        tid = res["transaktion_id"]
        row = lambda: [t for t in client.get(f"/books/{book}/transaktioner").json() if t["id"] == tid][0]
        r = row()
        assert r["ext_ref"] == "FAKT-2211" and r["amount_ore"] == 125000
        assert r["status"] == "pending" and r["supplier_id"] == sup
        # mark paid later
        client.post(f"/books/{book}/transaktioner/{tid}/pay", json={"payment_date": "2026-03-20"})
        assert row()["status"] == "paid"

    def test_pay_inkop_with_extra_momsfri_fee(self, client, book):
        # Pay a pending inköp and add a momsfri Klarna/Qliro-avgift booked to its own konto.
        goods = client.post(f"/books/{book}/categories",
                            json={"name": "Varor", "kind": "expense", "bas_konto": 4010}).json()["id"]
        feecat = client.post(f"/books/{book}/categories",
                             json={"name": "Betaltjänstavgift", "kind": "expense",
                                   "bas_konto": 6570}).json()["id"]
        res = client.post(f"/books/{book}/expenses", json={
            "category_id": goods, "trans_date": "2026-03-01",
            "lines": [{"rate_code": "25", "amount_ore": 100000, "inclusive": False}]}).json()
        tid = res["transaktion_id"]
        r = client.post(f"/books/{book}/transaktioner/{tid}/pay", json={
            "payment_date": "2026-03-20", "extra_fee_ore": 5000,
            "extra_fee_category_id": feecat})
        assert r.status_code == 200
        hb = {a["bas_konto"]: a for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[6570]["saldo_ore"] == 5000          # fee debited to 6570
        assert hb[1930]["saldo_ore"] == -(125000 + 5000)   # bank pays goods inc + fee
        assert hb[4010]["saldo_ore"] == 100000        # goods ex moms
        assert hb[2640]["saldo_ore"] == 25000         # ingående moms (unchanged by fee)
        # the fee is momsfri → it does not touch any output/moms box beyond ingående above
        # and the inköp total now includes the fee
        row = [t for t in client.get(f"/books/{book}/transaktioner").json() if t["id"] == tid][0]
        assert row["amount_ore"] == 125000 + 5000

    def test_pay_inkop_from_private_account(self, client, book):
        # Paying a firma cost with private money credits 2018 Egna insättningar, not 1930.
        goods = client.post(f"/books/{book}/categories",
                            json={"name": "Varor", "kind": "expense", "bas_konto": 4010}).json()["id"]
        tid = client.post(f"/books/{book}/expenses", json={
            "category_id": goods, "trans_date": "2026-03-01",
            "lines": [{"rate_code": "25", "amount_ore": 100000, "inclusive": False}]}).json()["transaktion_id"]
        r = client.post(f"/books/{book}/transaktioner/{tid}/pay",
                        json={"payment_date": "2026-03-20", "paid_account": "privat"})
        assert r.status_code == 200
        hb = {a["bas_konto"]: a for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[2018]["saldo_ore"] == -125000        # funded from private money (egna insättningar)
        assert 1930 not in hb                          # bank untouched
        assert hb[4010]["saldo_ore"] == 100000 and hb[2640]["saldo_ore"] == 25000

    def test_pay_inkop_from_bank_default(self, client, book):
        goods = client.post(f"/books/{book}/categories",
                            json={"name": "Varor", "kind": "expense", "bas_konto": 4010}).json()["id"]
        tid = client.post(f"/books/{book}/expenses", json={
            "category_id": goods, "trans_date": "2026-03-01",
            "lines": [{"rate_code": "25", "amount_ore": 100000, "inclusive": False}]}).json()["transaktion_id"]
        client.post(f"/books/{book}/transaktioner/{tid}/pay", json={"payment_date": "2026-03-20"})
        hb = {a["bas_konto"]: a for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[1930]["saldo_ore"] == -125000 and 2018 not in hb

    def test_payment_note_in_verifikation_text(self, client, book):
        goods = client.post(f"/books/{book}/categories",
                            json={"name": "Varor", "kind": "expense", "bas_konto": 4010}).json()["id"]
        tid = client.post(f"/books/{book}/expenses", json={
            "category_id": goods, "trans_date": "2026-03-01",
            "lines": [{"rate_code": "25", "amount_ore": 100000, "inclusive": False}]}).json()["transaktion_id"]
        client.post(f"/books/{book}/transaktioner/{tid}/pay",
                    json={"payment_date": "2026-03-20", "note": "OCR 4567 Klarna"})
        vers = client.get(f"/books/{book}/verifikationer-full").json()
        texts = " ".join(v.get("text", "") for v in vers)
        assert "OCR 4567 Klarna" in texts and "Utgift – OCR 4567 Klarna" in texts

    def test_extra_fee_needs_expense_category(self, client, book):
        goods = client.post(f"/books/{book}/categories",
                            json={"name": "Varor", "kind": "expense", "bas_konto": 4010}).json()["id"]
        inccat = client.post(f"/books/{book}/categories",
                             json={"name": "Sälj", "kind": "income", "bas_konto": 3001}).json()["id"]
        tid = client.post(f"/books/{book}/expenses", json={
            "category_id": goods, "trans_date": "2026-03-01",
            "lines": [{"rate_code": "25", "amount_ore": 100000, "inclusive": False}]}).json()["transaktion_id"]
        # an income category for the fee is rejected
        assert client.post(f"/books/{book}/transaktioner/{tid}/pay", json={
            "payment_date": "2026-03-20", "extra_fee_ore": 5000,
            "extra_fee_category_id": inccat}).status_code in (400, 409)

    def test_reverse_creates_rattelse(self, client, book):
        res = self._setup_income(client, book).json()
        vid = res["verifikation_id"]
        rev = client.post(f"/books/{book}/verifikationer/{vid}/reverse",
                          json={"reason": "fel"})
        assert rev.status_code == 201
        assert rev.json()["ver_number"] == 2

    def test_rebook_corrects_account_and_flags_corrected(self, client, book):
        wrong = client.post(f"/books/{book}/categories",
                            json={"name": "Fel", "kind": "expense", "bas_konto": 3003}).json()["id"]
        right = client.post(f"/books/{book}/categories",
                            json={"name": "Kontor", "kind": "expense", "bas_konto": 5460}).json()["id"]
        exp = client.post(f"/books/{book}/expenses",
                          json={"category_id": wrong, "lines": [{"rate_code": "25", "amount_ore": 1250}],
                                "trans_date": "2026-02-01", "paid_date": "2026-02-01"}).json()
        tid = exp["transaktion_id"]
        mlid = client.get(f"/books/{book}/transaktioner/{tid}/lines").json()[0]["id"]
        r = client.post(f"/books/{book}/transaktioner/{tid}/rebook",
                        json={"corrections": {str(mlid): {"category_id": right}}})
        assert r.status_code == 201
        # transaktion is now flagged corrected, and the synthetic clone is not listed
        txs = client.get(f"/books/{book}/transaktioner").json()
        assert len(txs) == 1 and txs[0]["corrected"] == 1
        # huvudbok: the wrong konto nets to 0, the right konto holds the ex-moms
        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb.get(3003, 0) == 0 and hb.get(5460) == 1000

    def test_rebook_to_momsfri_zeroes_moms(self, client, book):
        res = self._setup_income(client, book).json()          # 1250 incl 25% on 3001
        tid = res["transaktion_id"]
        mlid = client.get(f"/books/{book}/transaktioner/{tid}/lines").json()[0]["id"]
        client.post(f"/books/{book}/transaktioner/{tid}/rebook",
                    json={"corrections": {str(mlid): {"rate_code": "momsfri"}}})
        boxes = client.get(f"/books/{book}/reports/momsdeklaration",
                           params={"start": "2026-01-01", "end": "2026-12-31"}).json()["boxes"]
        assert boxes["10"] == 0                                  # utg moms nets out
        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb.get(3001) == -1250                            # full amount is income now

    def test_transaktioner_flags_invoice_backed_and_rut(self, client, book):
        # a plain expense: not invoice-backed, no rut -> the 'Rätta baskonto' button shows
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "M", "kind": "expense", "bas_konto": 5460}).json()["id"]
        client.post(f"/books/{book}/expenses",
                    json={"category_id": cat, "lines": [{"rate_code": "25", "amount_ore": 1250}],
                          "trans_date": "2026-02-01", "paid_date": "2026-02-01"})
        # an invoice's income transaktion IS invoice-backed -> button hidden client-side
        icat = client.post(f"/books/{book}/categories",
                           json={"name": "S", "kind": "income", "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "X"}).json()["kundnummer"]
        client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": icat, "invoice_date": "2026-02-01",
            "due_date": "2026-03-01",
            "lines": [{"description": "x", "quantity_centi": 100, "unit_price_ore": 100000,
                       "rate_code": "25"}]})
        txs = client.get(f"/books/{book}/transaktioner").json()
        by = {(t["direction"], bool(t["invoice_backed"])) for t in txs}
        assert ("in", False) in by            # the plain expense
        assert ("out", True) in by            # the invoice income

    def test_rebook_invoice_refused(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "T", "kind": "income", "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "X"}).json()["kundnummer"]
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-02-01",
            "due_date": "2026-03-01",
            "lines": [{"description": "x", "quantity_centi": 100, "unit_price_ore": 100000,
                       "rate_code": "25"}]}).json()
        client.post(f"/books/{book}/transaktioner/{inv['transaktion_id']}/pay",
                    json={"payment_date": "2026-02-02"})
        mlid = client.get(f"/books/{book}/transaktioner/{inv['transaktion_id']}/lines").json()[0]["id"]
        r = client.post(f"/books/{book}/transaktioner/{inv['transaktion_id']}/rebook",
                        json={"corrections": {str(mlid): {"category_id": cat}}})
        assert r.status_code == 409                              # fakturor -> kreditfaktura

    def test_manual_verifikation_and_ledger(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "Material", "kind": "expense", "bas_konto": 5460}).json()["id"]
        client.post(f"/books/{book}/expenses",
                    json={"category_id": cat, "lines": [{"rate_code": "25", "amount_ore": 1250}],
                          "trans_date": "2026-02-01", "paid_date": "2026-02-01"})
        # a balanced manual correction: move 100 kr 5460 -> 5410 (new konto, gets a name)
        res = client.post(f"/books/{book}/verifikationer/manual", json={
            "ver_date": "2026-02-05", "text": "Omföring",
            "postings": [{"bas_konto": 5460, "credit_ore": 10000},
                         {"bas_konto": 5410, "debit_ore": 10000,
                          "account_name": "Förbrukningsinventarier"}]})
        assert res.status_code == 201
        assert res.json()["ver_number"] == 2
        # grundbok shows the manual ver with its postings
        full = client.get(f"/books/{book}/verifikationer-full").json()
        manual = [v for v in full if v["ver_number"] == 2][0]
        assert len(manual["postings"]) == 2
        # huvudbok groups by konto with saldo
        hb = {a["bas_konto"]: a for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[5410]["saldo_ore"] == 10000
        assert hb[5460]["saldo_ore"] == 1000 - 10000    # 12.50 booked minus 100 moved out

    def test_manual_verifikation_must_balance_400(self, client, book):
        resp = client.post(f"/books/{book}/verifikationer/manual", json={
            "ver_date": "2026-02-05", "text": "Fel",
            "postings": [{"bas_konto": 1930, "debit_ore": 10000},
                         {"bas_konto": 3001, "credit_ore": 5000}]})
        assert resp.status_code == 400

    def test_manual_verifikation_period_lock_409(self, client, book):
        client.post(f"/books/{book}/period-locks",
                    json={"period_start": "2026-01-01", "period_end": "2026-03-31"})
        resp = client.post(f"/books/{book}/verifikationer/manual", json={
            "ver_date": "2026-02-05", "text": "Sent",
            "postings": [{"bas_konto": 1930, "debit_ore": 10000},
                         {"bas_konto": 1510, "credit_ore": 10000}]})
        assert resp.status_code == 409

    def test_year_end_accrual(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "Försäljning", "kind": "income", "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "ACME AB"}).json()["kundnummer"]
        # unpaid invoice dated in the closing year
        client.post(f"/books/{book}/incomes",
                    json={"customer_id": kid, "category_id": cat,
                          "lines": [{"rate_code": "25", "amount_ore": 1250}],
                          "trans_date": "2026-12-20"})
        resp = client.post(f"/books/{book}/year-end-accruals",
                           json={"fiscal_year_end": "2026-12-31"})
        assert resp.status_code == 201
        assert resp.json()["count"] == 1

    def test_synthetic_rows_hidden_from_default_list(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "Försäljning", "kind": "income", "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "ACME AB"}).json()["kundnummer"]
        client.post(f"/books/{book}/incomes",
                    json={"customer_id": kid, "category_id": cat,
                          "lines": [{"rate_code": "25", "amount_ore": 1250}],
                          "trans_date": "2026-12-20"})  # unpaid
        client.post(f"/books/{book}/year-end-accruals", json={"fiscal_year_end": "2026-12-31"})
        # accrual created 2 synthetic rows (periodisering + återföring)
        default = client.get(f"/books/{book}/transaktioner").json()
        full = client.get(f"/books/{book}/transaktioner",
                          params={"include_synthetic": True}).json()
        assert len(default) == 1                      # only the real pending invoice
        assert len(full) == 3
        assert all(t["note"] not in ("periodisering", "återföring", "rättelse") for t in default)

    def test_rut_claims_listing_and_skatteverket_payment(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "Städning", "kind": "income", "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "private", "first_name": "Anna",
                                "personnummer": "811218-9876"}).json()["kundnummer"]
        res = client.post(f"/books/{book}/incomes",
                          json={"customer_id": kid, "category_id": cat,
                                "lines": [{"rate_code": "25", "amount_ore": 10000}],
                                "trans_date": "2026-02-10", "paid_date": "2026-02-10",
                                "rut_amount_ore": 5000}).json()
        claim_id = res["rut_claim_id"]

        # listed and advanced to 'customer_paid' by the booked customer payment
        claims = client.get(f"/books/{book}/rut-claims").json()
        assert len(claims) == 1
        assert claims[0]["id"] == claim_id
        assert claims[0]["state"] == "customer_paid"

        pay = client.post(f"/books/{book}/rut/{claim_id}/skatteverket-payment",
                          json={"payment_date": "2026-04-01"})
        assert pay.status_code == 200
        claims = client.get(f"/books/{book}/rut-claims").json()
        assert claims[0]["state"] == "skatteverket_paid"
        assert claims[0]["skatteverket_payment_date"] == "2026-04-01"

    def test_multi_rate_expense_books_each_rate(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "Blandat", "kind": "expense", "bas_konto": 5460}).json()["id"]
        # One receipt with two moms rates (25 % + 12 %)
        res = client.post(f"/books/{book}/expenses",
                          json={"category_id": cat,
                                "lines": [{"rate_code": "25", "amount_ore": 1250},
                                          {"rate_code": "12", "amount_ore": 1120}],
                                "trans_date": "2026-02-01", "paid_date": "2026-02-01"})
        assert res.status_code == 201
        rep = client.get(f"/books/{book}/reports/momsdeklaration",
                         params={"start": "2026-01-01", "end": "2026-03-31"}).json()
        assert rep["boxes"]["48"] == 250 + 120     # ingående moms 25% + 12%


# ---------------------------------------------------------------------------
# Receipts (encrypted photo upload / fetch)
# ---------------------------------------------------------------------------

class TestReceipts:
    def _pending_expense(self, client, book) -> int:
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "Material", "kind": "expense", "bas_konto": 5460}).json()["id"]
        return client.post(f"/books/{book}/expenses",
                           json={"category_id": cat,
                                 "lines": [{"rate_code": "25", "amount_ore": 1250}],
                                 "trans_date": "2026-02-01"}).json()["transaktion_id"]

    def test_upload_list_and_fetch_image(self, client, book):
        import base64
        tid = self._pending_expense(client, book)
        raw = b"\x89PNG\r\n fake receipt \xff\x00\x10"
        up = client.post(f"/books/{book}/transaktioner/{tid}/receipts",
                         json={"image_base64": base64.b64encode(raw).decode(),
                               "mime": "image/png", "original_format": "paper"})
        assert up.status_code == 201
        rid = up.json()["id"]

        lst = client.get(f"/books/{book}/transaktioner/{tid}/receipts").json()
        assert len(lst) == 1 and lst[0]["mime"] == "image/png"

        img = client.get(f"/books/{book}/receipts/{rid}")
        assert img.status_code == 200
        assert img.headers["content-type"].startswith("image/png")
        assert img.content == raw          # decrypts back to the original bytes

    def test_upload_rejects_bad_base64(self, client, book):
        tid = self._pending_expense(client, book)
        resp = client.post(f"/books/{book}/transaktioner/{tid}/receipts",
                           json={"image_base64": "not base64!!!", "mime": "image/png"})
        assert resp.status_code == 400

    def test_delete_blocked_after_booking_409(self, client, book):
        import base64
        tid = self._pending_expense(client, book)
        rid = client.post(f"/books/{book}/transaktioner/{tid}/receipts",
                          json={"image_base64": base64.b64encode(b"x").decode(),
                                "mime": "image/png"}).json()["id"]
        client.post(f"/books/{book}/transaktioner/{tid}/pay",
                    json={"payment_date": "2026-02-05"})
        resp = client.delete(f"/books/{book}/receipts/{rid}")
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Key management — change passphrase + recovery key
# ---------------------------------------------------------------------------

class TestKeyManagement:
    def test_change_passphrase(self, client, book):
        assert client.post(f"/books/{book}/change-passphrase",
                           json={"old_passphrase": "correct-horse",
                                 "new_passphrase": "brand-new-pass"}).status_code == 200
        client.post(f"/books/{book}/lock")
        # old passphrase no longer works, new one does
        assert client.post(f"/books/{book}/unlock",
                           json={"passphrase": "correct-horse"}).status_code == 401
        assert client.post(f"/books/{book}/unlock",
                           json={"passphrase": "brand-new-pass"}).status_code == 200

    def test_wrong_old_passphrase_rejected_401(self, client, book):
        resp = client.post(f"/books/{book}/change-passphrase",
                           json={"old_passphrase": "nope", "new_passphrase": "x"})
        assert resp.status_code == 401

    def test_add_recovery_key_then_unlock_with_it(self, client, book):
        assert client.get(f"/books/{book}/recovery-key").json()["has_recovery_key"] is False
        rk = client.post(f"/books/{book}/recovery-key",
                         json={"passphrase": "correct-horse"})
        assert rk.status_code == 201
        key = rk.json()["recovery_key"]
        assert key and "-" in key
        assert client.get(f"/books/{book}/recovery-key").json()["has_recovery_key"] is True

        client.post(f"/books/{book}/lock")
        # recovery key unlocks even if the passphrase is forgotten
        assert client.post(f"/books/{book}/unlock-recovery",
                           json={"recovery_key": key}).status_code == 200

    def test_change_passphrase_keeps_recovery_key(self, client, book):
        key = client.post(f"/books/{book}/recovery-key",
                          json={"passphrase": "correct-horse"}).json()["recovery_key"]
        client.post(f"/books/{book}/change-passphrase",
                    json={"old_passphrase": "correct-horse", "new_passphrase": "p2"})
        client.post(f"/books/{book}/lock")
        # recovery slot survives a passphrase change
        assert client.post(f"/books/{book}/unlock-recovery",
                           json={"recovery_key": key}).status_code == 200


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

class TestReports:
    def test_momsdeklaration(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "Försäljning", "kind": "income",
                                "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "ACME AB"}).json()["kundnummer"]
        client.post(f"/books/{book}/incomes",
                    json={"customer_id": kid, "category_id": cat,
                          "lines": [{"rate_code": "25", "amount_ore": 1250}],
                          "trans_date": "2026-02-10", "paid_date": "2026-02-10"})
        rep = client.get(f"/books/{book}/reports/momsdeklaration",
                         params={"start": "2026-01-01", "end": "2026-03-31"}).json()
        assert rep["boxes"]["10"] == 250

    def test_forenklat_arsbokslut(self, client, book):
        inc = client.post(f"/books/{book}/categories",
                          json={"name": "Tjänster", "kind": "income", "bas_konto": 3011}).json()["id"]
        exp = client.post(f"/books/{book}/categories",
                          json={"name": "Material", "kind": "expense", "bas_konto": 5460}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "ACME AB"}).json()["kundnummer"]
        # sale 10 000 + moms, paid
        client.post(f"/books/{book}/incomes",
                    json={"customer_id": kid, "category_id": inc,
                          "lines": [{"rate_code": "25", "amount_ore": 1250000}],
                          "trans_date": "2026-03-01", "paid_date": "2026-03-05"})
        # expense 2 000 + moms, paid
        client.post(f"/books/{book}/expenses",
                    json={"category_id": exp, "lines": [{"rate_code": "25", "amount_ore": 250000}],
                          "trans_date": "2026-04-01", "paid_date": "2026-04-02"})
        # buy an inventarie via a manual verifikation (bank -> 1220)
        client.post(f"/books/{book}/verifikationer/manual", json={
            "ver_date": "2026-05-01", "text": "Inköp inventarie",
            "postings": [{"bas_konto": 1220, "debit_ore": 400000, "account_name": "Inventarier"},
                         {"bas_konto": 1930, "credit_ore": 400000}]})

        ab = client.get(f"/books/{book}/reports/arsbokslut",
                        params={"start": "2026-01-01", "end": "2026-12-31"}).json()
        assert ab["resultat"]["R1"]["value_ore"] == 1000000     # income ex-moms
        assert ab["resultat"]["R6"]["value_ore"] == 200000      # material -> övriga externa
        assert ab["arets_resultat_ore"] == 800000
        assert ab["balans"]["B4"]["value_ore"] == 400000        # maskiner/inventarier
        assert ab["balans"]["B9"]["value_ore"] == 600000        # bank 12500-2500-4000
        assert ab["balans"]["B10"]["value_ore"] == 800000       # eget kapital = result
        assert ab["balans"]["B14"]["value_ore"] == 200000       # moms skuld 2500-500
        assert ab["summa_tillgangar_ore"] == ab["summa_ek_skulder_ore"]
        assert ab["balanserar"] is True

    def test_sie_export_plaintext(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "Försäljning", "kind": "income",
                                "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "ACME AB"}).json()["kundnummer"]
        client.post(f"/books/{book}/incomes",
                    json={"customer_id": kid, "category_id": cat,
                          "lines": [{"rate_code": "25", "amount_ore": 1250}],
                          "trans_date": "2026-02-10", "paid_date": "2026-02-10"})
        resp = client.get(f"/books/{book}/reports/sie", params={"company_name": "Min Firma"})
        assert resp.status_code == 200
        assert "#VER A 1 20260210" in resp.text

    def test_tax_estimate_and_config(self, client, book):
        cfg = client.get(f"/books/{book}/tax-config").json()
        assert cfg["kommunal_skattesats_pct_centi"] == 3237      # default
        assert cfg["prisbasbelopp_ore"] == 5920000              # 2026 pbb
        # edit rates (kommun + salary for the total overview)
        upd = client.put(f"/books/{book}/tax-config",
                         json={"kommunal_skattesats_pct_centi": 3055,
                               "ovrig_forvarvsinkomst_ore": 46200000}).json()
        assert upd["kommunal_skattesats_pct_centi"] == 3055
        assert upd["ovrig_forvarvsinkomst_ore"] == 46200000
        # a sale, then the year-end estimate
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "Försäljning", "kind": "income", "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "ACME AB"}).json()["kundnummer"]
        client.post(f"/books/{book}/incomes",
                    json={"customer_id": kid, "category_id": cat,
                          "lines": [{"rate_code": "25", "amount_ore": 40000000, "inclusive": False}],
                          "trans_date": "2026-03-01", "paid_date": "2026-03-01"})
        est = client.get(f"/books/{book}/reports/tax",
                         params={"start": "2026-01-01", "end": "2026-12-31"}).json()
        assert est["overskott_ore"] == 40000000                 # ex-moms income
        assert est["moms_ore"] == 10000000                      # utgående, no purchases
        assert est["egenavgifter"]["netto_ore"] > 0
        assert [l["key"] for l in est["lines"]][:2] == ["moms", "egenavgifter"]
        assert est["firma_total_ore"] > 0
        assert est["overview"]["salary_skatt_ore"] > 0          # salary factored in


# ---------------------------------------------------------------------------
# Export / import over the API
# ---------------------------------------------------------------------------

class TestExportImport:
    def test_export_then_import_roundtrip(self, client, book, tmp_path):
        # seed a customer, export, import to a new path, unlock, verify
        client.post(f"/books/{book}/customers",
                    json={"type": "private", "first_name": "Anna",
                          "personnummer": "811218-9876"})
        out = str(tmp_path / "export.buyn")
        assert client.post(f"/books/{book}/export", json={"out_path": out}).status_code == 200

        dest = str(tmp_path / "restored.db")
        rec = client.post("/books/import",
                          json={"bundle_path": out, "dest_db_path": dest,
                                "display_name": "Restored"})
        assert rec.status_code == 201
        new_id = rec.json()["id"]
        client.post(f"/books/{new_id}/unlock", json={"passphrase": "correct-horse"})
        got = client.get(f"/books/{new_id}/customers/1").json()
        assert got["personnummer"] == "811218-9876"


# ---------------------------------------------------------------------------
# Invoices (faktura)
# ---------------------------------------------------------------------------

class TestInvoices:
    def _setup(self, client, book):
        client.put(f"/books/{book}/company",
                   json={"name": "Min Firma AB", "org_nr": "556677-8899", "f_skatt": 1})
        client.post(f"/books/{book}/payment-methods", json={"label": "Swish", "value": "1234567890"})
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "Tjänster", "kind": "income", "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "private", "first_name": "Anna", "last_name": "Svensson",
                                "personnummer": "811218-9876", "address": "Storgatan 1"}).json()["kundnummer"]
        return cat, kid

    def test_company_and_payment_methods(self, client, book):
        self._setup(client, book)
        assert client.get(f"/books/{book}/company").json()["name"] == "Min Firma AB"
        assert client.get(f"/books/{book}/payment-methods").json()[0]["label"] == "Swish"

    def test_payment_methods_append_and_reorder(self, client, book):
        a = client.post(f"/books/{book}/payment-methods", json={"label": "Swish", "value": "1"}).json()["id"]
        b = client.post(f"/books/{book}/payment-methods", json={"label": "Bankgiro", "value": "2"}).json()["id"]
        c = client.post(f"/books/{book}/payment-methods", json={"label": "IBAN", "value": "3"}).json()["id"]
        order = [m["label"] for m in client.get(f"/books/{book}/payment-methods").json()]
        assert order == ["Swish", "Bankgiro", "IBAN"]         # new ones append at the end
        r = client.post(f"/books/{book}/payment-methods/reorder", json={"ordered_ids": [c, a, b]})
        assert r.status_code == 200
        order2 = [m["label"] for m in client.get(f"/books/{book}/payment-methods").json()]
        assert order2 == ["IBAN", "Swish", "Bankgiro"]

    def test_inkop_pay_methods_editable_list(self, client, book):
        # defaults present until edited
        d = client.get(f"/books/{book}/inkop-pay-methods").json()["methods"]
        assert "Qliro" in d and "Klarna delbetalning" in d
        # set a custom list (trimmed + de-duplicated, order preserved)
        r = client.put(f"/books/{book}/inkop-pay-methods",
                       json={"methods": [" Qliro ", "Mitt Amex", "Qliro", ""]})
        assert r.status_code == 200
        assert r.json()["methods"] == ["Qliro", "Mitt Amex"]
        assert client.get(f"/books/{book}/inkop-pay-methods").json()["methods"] == ["Qliro", "Mitt Amex"]

    def test_unpaid_invoice_uses_live_payment_methods(self, client, book):
        cat, kid = self._setup(client, book)              # seeds a "Swish" method
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-15",
            "due_date": "2026-04-15",
            "lines": [{"description": "IT", "quantity_centi": 100, "unit_price_ore": 100000,
                       "rate_code": "25"}]}).json()
        iid = inv["invoice_id"]
        # add a NEW payment method AFTER the invoice was issued
        client.post(f"/books/{book}/payment-methods", json={"label": "Bankgiro", "value": "999-8888"})
        pms = [m["label"] for m in client.get(f"/books/{book}/invoices/{iid}").json()["payment_methods"]]
        assert "Bankgiro" in pms and "Swish" in pms        # unpaid -> live methods
        # the PDF regenerates fine
        assert client.get(f"/books/{book}/invoices/{iid}/pdf").content[:4] == b"%PDF"
        # once paid, it keeps the frozen snapshot (no Bankgiro)
        client.post(f"/books/{book}/invoices/{iid}/pay", json={"date": "2026-03-20"})
        pms2 = [m["label"] for m in client.get(f"/books/{book}/invoices/{iid}").json()["payment_methods"]]
        assert pms2 == ["Swish"]

    def test_support_disabled_earns_nothing(self, client, book):
        cat, kid = self._setup(client, book)
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-15",
            "due_date": "2026-04-15", "support_enabled": False,
            "lines": [{"description": "IT", "quantity_centi": 100,
                       "unit_price_ore": round(624900 / 1.25), "rate_code": "25"}]}).json()
        got = client.get(f"/books/{book}/invoices/{inv['invoice_id']}").json()
        assert got["support_minutes_earned"] == 0 and got["support_enabled"] == 0
        assert got["support_expiry_date"] is None          # PDF skips the note
        assert client.get(f"/books/{book}/customers/{kid}/support").json()["earned_active_minutes"] == 0

    def test_support_time_bank(self, client, book):
        cat, kid = self._setup(client, book)
        # inc 2 000 kr -> 30 min support (15 min per full 1 000 kr)
        client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-15",
            "due_date": "2026-04-15",
            "lines": [{"description": "IT", "quantity_centi": 100,
                       "unit_price_ore": round(200000 / 1.25), "rate_code": "25"}]})
        s = client.get(f"/books/{book}/customers/{kid}/support").json()
        assert s["earned_active_minutes"] == 30 and s["remaining_minutes"] == 30
        assert len(s["active_invoices"]) == 1
        # quick-deduct 60, then add 20 -> net used 40 vs earned 30; remaining floors at 0
        assert client.post(f"/books/{book}/customers/{kid}/support",
                           json={"minutes": 60, "kind": "deduction"}).status_code == 201
        client.post(f"/books/{book}/customers/{kid}/support",
                    json={"minutes": 20, "kind": "addition", "note": "bonus"})
        s = client.get(f"/books/{book}/customers/{kid}/support").json()
        assert s["remaining_minutes"] == 0        # floored, though net used (40) > earned (30)
        assert s["used_minutes"] == 40            # the real over-use is still visible
        assert len(s["ledger"]) == 2 and s["ledger"][0]["kind"] == "addition"
        # the PDF carries the support text block
        pdf = client.get(f"/books/{book}/invoices/1/pdf")
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"

    def test_edit_and_delete_payment_method(self, client, book):
        pid = client.post(f"/books/{book}/payment-methods",
                          json={"label": "Swish", "value": "111"}).json()["id"]
        # edit name + number
        client.patch(f"/books/{book}/payment-methods/{pid}",
                     json={"label": "Bankgiro", "value": "123-4567"})
        m = client.get(f"/books/{book}/payment-methods").json()[0]
        assert m["label"] == "Bankgiro" and m["value"] == "123-4567"
        # inactivate
        client.patch(f"/books/{book}/payment-methods/{pid}", json={"active": 0})
        assert client.get(f"/books/{book}/payment-methods").json()[0]["active"] == 0
        # delete
        assert client.delete(f"/books/{book}/payment-methods/{pid}").status_code == 200
        assert client.get(f"/books/{book}/payment-methods").json() == []

    def test_create_list_get_invoice(self, client, book):
        cat, kid = self._setup(client, book)
        resp = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31", "payment_terms": "30 dagar",
            "lines": [{"description": "Konsult", "quantity_centi": 200, "unit": "h",
                       "unit_price_ore": 100000, "rate_code": "25"}]})
        assert resp.status_code == 201
        inv = resp.json()
        assert inv["invoice_number"] == 1 and inv["inc_moms_ore"] == 250000
        lst = client.get(f"/books/{book}/invoices").json()
        assert len(lst) == 1 and lst[0]["state"] == "pending"
        got = client.get(f"/books/{book}/invoices/{inv['invoice_id']}").json()
        assert got["seller"]["name"] == "Min Firma AB"
        assert got["buyer"]["first_name"] == "Anna"
        assert got["payment_methods"][0]["label"] == "Swish"

    def test_create_invoice_line_discount(self, client, book):
        cat, kid = self._setup(client, book)
        # 1 000 kr ex, 15 % rabatt -> 850 ex; moms 25 % = 212.50; inc = 1 062.50
        resp = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "Konsult", "quantity_centi": 100, "unit_price_ore": 100000,
                       "rate_code": "25", "discount_pct_centi": 1500}]})
        assert resp.status_code == 201
        inv = resp.json()
        assert inv["ex_moms_ore"] == 85000 and inv["inc_moms_ore"] == 106250
        line = client.get(f"/books/{book}/invoices/{inv['invoice_id']}").json()["lines"][0]
        assert line["discount_pct_centi"] == 1500
        assert line["unit_price_ore"] == 100000     # list price kept for the PDF

    def test_invoice_with_rut_household_split(self, client, book):
        cat, kid = self._setup(client, book)
        # ex 1 000 000 @ 25% -> inc 1 250 000; RUT pot = 50% incl moms = 625 000.
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "Städ", "quantity_centi": 100, "unit_price_ore": 1000000,
                       "rate_code": "25", "reduction_type": "rut"}],
            "recipients": [{"first_name": "Anna", "last_name": "Svensson",
                            "personnummer": "811218-9876", "share_pct": 60},
                           {"first_name": "Björn", "last_name": "Svensson",
                            "personnummer": "19811218-9876", "share_pct": 40}]}).json()
        assert inv["rut_total_ore"] == 625000
        got = client.get(f"/books/{book}/invoices/{inv['invoice_id']}").json()
        assert len(got["recipients"]) == 2
        assert got["recipients"][0]["rut_amount_ore"] == 375000

    def test_invoice_rot_and_household_relations(self, client, book):
        cat, kid = self._setup(client, book)
        # add a household member and link them
        bjorn = client.post(f"/books/{book}/customers", json={
            "type": "private", "first_name": "Björn", "last_name": "Svensson"}).json()["kundnummer"]
        rel = client.post(f"/books/{book}/customers/{kid}/relations",
                          json={"other_kundnummer": bjorn})
        assert rel.status_code == 201
        rels = client.get(f"/books/{book}/customers/{kid}/relations").json()
        assert any(r["kundnummer"] == bjorn for r in rels)
        # ROT invoice with the linked member as recipient (personnummer saved on them)
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "Snickeri", "quantity_centi": 100, "unit_price_ore": 1000000,
                       "rate_code": "25", "reduction_type": "rot"}],
            "recipients": [{"customer_id": bjorn, "personnummer": "19811218-9876",
                            "share_pct": 100}]}).json()
        assert inv["rot_total_ore"] == 375000        # ROT 30% incl moms
        # personnummer got saved onto Björn's customer record
        assert client.get(f"/books/{book}/customers/{bjorn}").json()["personnummer"] == "8112189876"
        # unlink works
        assert client.delete(f"/books/{book}/customers/{kid}/relations/{bjorn}").status_code == 200
        assert client.get(f"/books/{book}/customers/{kid}/relations").json() == []

    def test_separate_shares_and_cap_endpoint(self, client, book):
        cat, kid = self._setup(client, book)
        member = client.post(f"/books/{book}/customers", json={
            "type": "private", "first_name": "Mem", "last_name": "M"}).json()["kundnummer"]
        # big RUT line pushes the member over the 75 000 kr cap; ROT share differs
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "Städ", "quantity_centi": 100, "unit_price_ore": 20000000,
                       "rate_code": "25", "reduction_type": "rut"}],
            "recipients": [{"customer_id": member, "personnummer": "19811218-9876",
                            "rut_share_pct": 100, "rot_share_pct": 0}]}).json()
        assert any(w["over_cap"] for w in inv["cap_warnings"])
        cap = client.get(f"/books/{book}/customers/{member}/husavdrag-cap/2026").json()
        assert cap["used_ore"] == 12500000 and cap["over_cap"] is True

    def test_invoice_pdf_download(self, client, book):
        cat, kid = self._setup(client, book)
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "Konsult", "quantity_centi": 100,
                       "unit_price_ore": 100000, "rate_code": "25"}]}).json()
        pdf = client.get(f"/books/{book}/invoices/{inv['invoice_id']}/pdf")
        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.content[:4] == b"%PDF"

    def test_invoice_draft_save_continue_finalize(self, client, book):
        cat, kid = self._setup(client, book)
        payload = {"customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
                   "due_date": "2026-03-31",
                   "lines": [{"description": "Konsult", "quantity_centi": 100,
                              "unit_price_ore": 100000, "rate_code": "25"}]}
        # save
        d = client.post(f"/books/{book}/invoice-drafts", json={"payload": payload})
        assert d.status_code == 201
        did = d.json()["id"]
        lst = client.get(f"/books/{book}/invoice-drafts").json()
        assert len(lst) == 1 and lst[0]["line_count"] == 1 and lst[0]["total_ore"] == 125000
        # continue: payload round-trips (incl. it being stored encrypted at rest)
        full = client.get(f"/books/{book}/invoice-drafts/{did}").json()
        assert full["payload"]["lines"][0]["description"] == "Konsult"
        # update
        client.put(f"/books/{book}/invoice-drafts/{did}",
                   json={"payload": {**payload, "note": "klar snart"}})
        assert client.get(f"/books/{book}/invoice-drafts/{did}").json()["payload"]["note"] == "klar snart"
        # finalize: issue the invoice, then drop the draft
        inv = client.post(f"/books/{book}/invoices", json=payload).json()
        assert inv["invoice_number"] == 1
        client.delete(f"/books/{book}/invoice-drafts/{did}")
        assert client.get(f"/books/{book}/invoice-drafts").json() == []

    def test_rut_invoice_keeps_skatteverket_button_after_customer_payment(self, client, book):
        cat, kid = self._setup(client, book)
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "Städ", "quantity_centi": 100, "unit_price_ore": 1000000,
                       "rate_code": "25", "reduction_type": "rut"}],
            "recipients": [{"customer_id": kid, "share_pct": 100}]}).json()
        tid = inv["transaktion_id"]
        row = lambda: [x for x in client.get(f"/books/{book}/invoices").json()
                       if x["id"] == inv["invoice_id"]][0]
        assert row()["rut_claim_state"] == "pending"
        # book the customer payment -> the SKV husavdrag step becomes available. The
        # invoice is NOT fully settled yet: Skatteverket still owes the husavdrag part.
        client.post(f"/books/{book}/transaktioner/{tid}/pay", json={"payment_date": "2026-03-10"})
        r = row()
        assert r["state"] == "awaiting_rut" and r["rut_claim_state"] == "customer_paid"
        claim_id = r["rut_claim_id"]
        assert claim_id is not None
        # book the Skatteverket payout -> now fully paid
        client.post(f"/books/{book}/rut/{claim_id}/skatteverket-payment",
                    json={"payment_date": "2026-04-15"})
        assert row()["rut_claim_state"] == "skatteverket_paid"
        assert row()["state"] == "paid"

    def test_rut_next_reference_continues_sequence(self, client, book):
        cat, kid = self._setup(client, book)
        assert client.get(f"/books/{book}/rut-next-reference").json()["reference"] == "RUT1"

        def rut_invoice_paid_with_ref(ref):
            inv = client.post(f"/books/{book}/invoices", json={
                "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
                "due_date": "2026-03-31",
                "lines": [{"description": "Städ", "quantity_centi": 100, "unit_price_ore": 1000000,
                           "rate_code": "25", "reduction_type": "rut"}],
                "recipients": [{"customer_id": kid, "share_pct": 100}]}).json()
            client.post(f"/books/{book}/transaktioner/{inv['transaktion_id']}/pay",
                        json={"payment_date": "2026-03-10"})
            claim = [x for x in client.get(f"/books/{book}/invoices").json()
                     if x["id"] == inv["invoice_id"]][0]["rut_claim_id"]
            client.post(f"/books/{book}/rut/{claim}/skatteverket-payment",
                        json={"payment_date": "2026-04-15", "reference": ref})

        rut_invoice_paid_with_ref("RUT1")
        assert client.get(f"/books/{book}/rut-next-reference").json()["reference"] == "RUT2"
        # a manual jump (e.g. RUT4) is honoured — next continues from the max
        rut_invoice_paid_with_ref("RUT4")
        assert client.get(f"/books/{book}/rut-next-reference").json()["reference"] == "RUT5"

    def test_offert_from_draft_keeps_draft_and_numbers_sequentially(self, client, book):
        cat, kid = self._setup(client, book)
        payload = {"customer_id": kid, "invoice_date": "2026-03-01",
                   "lines": [{"description": "Jobb", "quantity_centi": 100,
                              "unit_price_ore": 100000, "rate_code": "25", "category_id": cat}],
                   "recipients": []}
        did = client.post(f"/books/{book}/invoice-drafts", json={"payload": payload}).json()["id"]
        # create an offert from the draft — the draft is kept
        o1 = client.post(f"/books/{book}/offerter", json={"draft_id": did}).json()
        assert o1["offert_number"] == 1 and o1["inc_moms_ore"] == 125000
        assert len(client.get(f"/books/{book}/invoice-drafts").json()) == 1   # draft kept
        # a second offert increments the number (own sequence)
        o2 = client.post(f"/books/{book}/offerter", json={"draft_id": did}).json()
        assert o2["offert_number"] == 2
        lst = client.get(f"/books/{book}/offerter").json()
        assert [o["offert_number"] for o in lst] == [1, 2]
        assert lst[0]["inc_moms_ore"] == 125000
        # the offert renders a PDF (OFFERT document)
        pdf = client.get(f"/books/{book}/offerter/{o1['offert_id']}/pdf")
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"

    def test_offert_to_invoice_once(self, client, book):
        cat, kid = self._setup(client, book)
        o = client.post(f"/books/{book}/offerter", json={"payload": {
            "customer_id": kid, "invoice_date": "2026-03-01",
            "lines": [{"description": "Jobb", "quantity_centi": 100, "unit_price_ore": 100000,
                       "rate_code": "25", "category_id": cat}], "recipients": []}}).json()
        inv = client.post(f"/books/{book}/offerter/{o['offert_id']}/create-invoice",
                          json={"invoice_date": "2026-04-01", "due_date": "2026-05-01"}).json()
        assert inv["invoice_number"] == 1 and inv["inc_moms_ore"] == 125000
        # the offert now links to the faktura
        row = client.get(f"/books/{book}/offerter").json()[0]
        assert row["invoice_id"] == inv["invoice_id"] and row["invoice_number"] == 1
        # a real, bookable invoice exists
        assert any(x["id"] == inv["invoice_id"] for x in client.get(f"/books/{book}/invoices").json())
        # invoicing the same offert again is refused
        again = client.post(f"/books/{book}/offerter/{o['offert_id']}/create-invoice", json={})
        assert again.status_code == 409

    def test_offert_new_versions_preserved_and_suffixed(self, client, book):
        cat, kid = self._setup(client, book)
        o = client.post(f"/books/{book}/offerter", json={"payload": {
            "customer_id": kid, "invoice_date": "2026-03-01",
            "lines": [{"description": "Jobb", "quantity_centi": 100, "unit_price_ore": 100000,
                       "rate_code": "25", "category_id": cat}], "recipients": []}}).json()
        v1 = client.post(f"/books/{book}/offerter/{o['offert_id']}/versions")
        assert v1.status_code == 201 and v1.json()["offert_number"] == "1-1"
        v2 = client.post(f"/books/{book}/offerter/{o['offert_id']}/versions").json()
        assert v2["offert_number"] == "1-2"
        # original + both versions are all kept, with suffixed display numbers
        nums = sorted(r["display_number"] for r in client.get(f"/books/{book}/offerter").json())
        assert nums == ["1", "1-1", "1-2"]

    def test_offert_from_payload_without_draft(self, client, book):
        cat, kid = self._setup(client, book)
        o = client.post(f"/books/{book}/offerter", json={"payload": {
            "customer_id": kid, "invoice_date": "2026-03-01",
            "lines": [{"description": "X", "quantity_centi": 100, "unit_price_ore": 50000,
                       "rate_code": "25", "category_id": cat}], "recipients": []}}).json()
        assert o["offert_number"] == 1 and o["inc_moms_ore"] == 62500
        assert len(client.get(f"/books/{book}/invoice-drafts").json()) == 0   # no draft created

    def test_skatteverket_partial_payout_creates_followup_via_api(self, client, book):
        cat, kid = self._setup(client, book)
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "Städ", "quantity_centi": 100, "unit_price_ore": 1000000,
                       "rate_code": "25", "reduction_type": "rut"}],
            "recipients": [{"customer_id": kid, "share_pct": 100}]}).json()
        tid = inv["transaktion_id"]
        client.post(f"/books/{book}/transaktioner/{tid}/pay", json={"payment_date": "2026-03-10"})
        claim_id = [x for x in client.get(f"/books/{book}/invoices").json()
                    if x["id"] == inv["invoice_id"]][0]["rut_claim_id"]
        H = inv["rut_total_ore"] + inv["rot_total_ore"]

        # preview flags the big underpayment as a partial payout
        prev = client.post(f"/books/{book}/rut/{claim_id}/skatteverket-preview",
                           json={"received_ore": H - 50000})
        assert prev.json()["interpretation"] == "partial"
        # without an explicit mode the backend refuses (so the UI must confirm)
        refused = client.post(f"/books/{book}/rut/{claim_id}/skatteverket-payment",
                              json={"payment_date": "2026-04-15", "received_ore": H - 50000})
        assert refused.status_code == 409
        # confirmed -> a linked follow-up invoice appears with no moms
        ok = client.post(f"/books/{book}/rut/{claim_id}/skatteverket-payment",
                         json={"payment_date": "2026-04-15", "received_ore": H - 50000,
                               "mode": "partial"}).json()
        fid = ok["shortfall_invoice_id"]
        assert fid is not None
        followup = [x for x in client.get(f"/books/{book}/invoices").json() if x["id"] == fid][0]
        assert followup["husavdrag_shortfall_ore"] == 50000
        assert followup["parent_invoice_id"] == inv["invoice_id"]
        # and it is payable (settles the customer receivable)
        paid = client.post(f"/books/{book}/invoices/{fid}/pay", json={"date": "2026-05-01"})
        assert paid.status_code == 201
        assert paid.json()["outstanding_ore"] == 0

    def test_skatteverket_reference_and_kvittens(self, client, book):
        import base64
        cat, kid = self._setup(client, book)
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "Städ", "quantity_centi": 100, "unit_price_ore": 1000000,
                       "rate_code": "25", "reduction_type": "rut"}],
            "recipients": [{"customer_id": kid, "share_pct": 100}]}).json()
        client.post(f"/books/{book}/transaktioner/{inv['transaktion_id']}/pay",
                    json={"payment_date": "2026-03-10"})
        claim_id = [x for x in client.get(f"/books/{book}/invoices").json()
                    if x["id"] == inv["invoice_id"]][0]["rut_claim_id"]
        # book the payout with a reference name (the RUT begäran)
        client.post(f"/books/{book}/rut/{claim_id}/skatteverket-payment",
                    json={"payment_date": "2026-04-15", "reference": "RUT1"})
        claim = [c for c in client.get(f"/books/{book}/rut-claims").json() if c["id"] == claim_id][0]
        assert claim["skatteverket_reference"] == "RUT1"
        # upload Skatteverket's kvittens (a small PDF) and list it back
        raw = b"%PDF-1.4 fake kvittens"
        up = client.post(f"/books/{book}/rut/{claim_id}/receipt",
                         json={"image_base64": base64.b64encode(raw).decode(),
                               "mime": "application/pdf"})
        assert up.status_code == 201
        receipts = client.get(f"/books/{book}/rut/{claim_id}/receipts").json()
        assert len(receipts) == 1 and receipts[0]["mime"] == "application/pdf"
        # the raw bytes come back decrypted intact
        rid = receipts[0]["id"]
        got = client.get(f"/books/{book}/receipts/{rid}")
        assert got.content == raw
        # it does NOT leak into the sale transaktion's own receipt list
        sale = client.get(f"/books/{book}/transaktioner/{inv['transaktion_id']}/receipts").json()
        assert all(r["id"] != rid for r in sale)

    def test_draft_payload_encrypted_at_rest(self, client, book, tmp_path):
        cat, kid = self._setup(client, book)
        client.post(f"/books/{book}/invoice-drafts", json={"payload": {
            "customer_id": kid, "recipients": [{"personnummer": "811218-9876"}], "lines": []}})
        # the raw DB must not contain the personnummer in cleartext
        import sqlite3, glob
        dbfile = glob.glob(str(tmp_path / "**" / "*.db"), recursive=True)[0]
        raw = sqlite3.connect(dbfile).execute(
            "SELECT payload_enc FROM invoice_draft").fetchone()[0]
        assert "811218-9876" not in raw


class TestLogo:
    def _b64png(self):
        import base64, io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (200, 80), (20, 90, 170)).save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    def test_logo_upload_get_delete(self, client, book):
        assert client.get(f"/books/{book}/logo").status_code == 404      # none yet
        assert client.put(f"/books/{book}/logo",
                          json={"image_base64": self._b64png()}).status_code == 200
        assert client.get(f"/books/{book}/company").json()["has_logo"] is True
        img = client.get(f"/books/{book}/logo")
        assert img.status_code == 200
        assert img.headers["content-type"] == "image/png"
        assert img.content[:4] == b"\x89PNG"
        assert client.delete(f"/books/{book}/logo").status_code == 200
        assert client.get(f"/books/{book}/logo").status_code == 404

    def test_logo_appears_on_invoice_pdf(self, client, book):
        client.put(f"/books/{book}/logo", json={"image_base64": self._b64png()})
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "T", "kind": "income", "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "private", "first_name": "A", "last_name": "B",
                                "personnummer": "811218-9876"}).json()["kundnummer"]
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "X", "quantity_centi": 100,
                       "unit_price_ore": 100000, "rate_code": "25"}]}).json()
        pdf = client.get(f"/books/{book}/invoices/{inv['invoice_id']}/pdf")
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"

    def test_logo_rejects_bad_base64(self, client, book):
        resp = client.put(f"/books/{book}/logo", json={"image_base64": "not base64!!!"})
        assert resp.status_code == 400


class TestInvoiceLifecycleApi:
    def _inv(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "T", "kind": "income", "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "private", "first_name": "A", "last_name": "B",
                                "personnummer": "811218-9876"}).json()["kundnummer"]
        return client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "X", "quantity_centi": 100,
                       "unit_price_ore": 100000, "rate_code": "25"}]}).json()

    def test_pay_then_state_paid(self, client, book):
        inv = self._inv(client, book)
        client.post(f"/books/{book}/invoices/{inv['invoice_id']}/pay",
                    json={"date": "2026-03-10"})
        lst = client.get(f"/books/{book}/invoices").json()
        assert lst[0]["state"] == "paid" and lst[0]["outstanding_ore"] == 0

    def test_partial_payment(self, client, book):
        inv = self._inv(client, book)   # inc 125000
        r = client.post(f"/books/{book}/invoices/{inv['invoice_id']}/pay",
                        json={"amount_ore": 50000, "date": "2026-03-10"})
        assert r.status_code == 201 and r.json()["outstanding_ore"] == 75000
        assert client.get(f"/books/{book}/invoices").json()[0]["state"] == "partial"
        client.post(f"/books/{book}/invoices/{inv['invoice_id']}/pay", json={"date": "2026-03-20"})
        assert client.get(f"/books/{book}/invoices").json()[0]["state"] == "paid"

    def test_refund(self, client, book):
        inv = self._inv(client, book)
        client.post(f"/books/{book}/invoices/{inv['invoice_id']}/pay", json={"date": "2026-03-10"})
        r = client.post(f"/books/{book}/invoices/{inv['invoice_id']}/refund",
                        json={"amount_ore": 25000, "date": "2026-03-15"})
        assert r.status_code == 201
        assert client.get(f"/books/{book}/invoices").json()[0]["outstanding_ore"] == 25000

    def test_makulera_unpaid(self, client, book):
        inv = self._inv(client, book)
        assert client.post(f"/books/{book}/invoices/{inv['invoice_id']}/cancel").status_code == 201
        assert client.get(f"/books/{book}/invoices").json()[0]["state"] == "cancelled"

    def test_makulera_paid_is_409(self, client, book):
        inv = self._inv(client, book)
        client.post(f"/books/{book}/invoices/{inv['invoice_id']}/pay", json={"date": "2026-03-10"})
        assert client.post(f"/books/{book}/invoices/{inv['invoice_id']}/cancel").status_code == 409

    def test_update_unpaid_invoice_keeps_number(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "T", "kind": "income", "bas_konto": 3001}).json()["id"]
        cat2 = client.post(f"/books/{book}/categories",
                           json={"name": "T2", "kind": "income", "bas_konto": 3002}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "private", "first_name": "A", "last_name": "B",
                                "personnummer": "811218-9876"}).json()["kundnummer"]
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "X", "quantity_centi": 100, "unit_price_ore": 100000,
                       "rate_code": "25"}]}).json()
        num = inv["invoice_number"]
        # adjust: new category, two lines, different amount
        r = client.put(f"/books/{book}/invoices/{inv['invoice_id']}", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-02",
            "due_date": "2026-04-02",
            "lines": [{"description": "Y", "quantity_centi": 200, "unit_price_ore": 80000,
                       "rate_code": "25", "category_id": cat2},
                      {"description": "Z", "quantity_centi": 100, "unit_price_ore": 20000,
                       "rate_code": "6", "category_id": cat}]})
        assert r.status_code == 200
        assert r.json()["invoice_number"] == num          # same fakturanummer
        got = client.get(f"/books/{book}/invoices/{r.json()['invoice_id']}").json()
        assert len(got["lines"]) == 2 and got["invoice_date"] == "2026-03-02"
        assert got["state"] == "pending"
        # the number series is unbroken: the next invoice is num+1
        nxt = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-05",
            "due_date": "2026-04-05",
            "lines": [{"description": "N", "quantity_centi": 100, "unit_price_ore": 10000,
                       "rate_code": "25"}]}).json()
        assert nxt["invoice_number"] == num + 1

    def test_update_paid_invoice_refused(self, client, book):
        inv = self._inv(client, book)
        client.post(f"/books/{book}/invoices/{inv['invoice_id']}/pay", json={"date": "2026-03-10"})
        r = client.put(f"/books/{book}/invoices/{inv['invoice_id']}", json={
            "customer_id": inv["customer_id"] if "customer_id" in inv else 1,
            "category_id": 1, "invoice_date": "2026-03-01", "due_date": "2026-03-31",
            "lines": [{"description": "X", "quantity_centi": 100, "unit_price_ore": 100000,
                       "rate_code": "25", "category_id": 1}]})
        assert r.status_code == 409          # booked/paid -> kreditera instead

    def test_update_unpaid_invoice_restocks_and_reconsumes(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "T", "kind": "income", "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "Y AB"}).json()["kundnummer"]
        aid = client.post(f"/books/{book}/articles", json={
            "description": "Widget", "prefix": "2000", "unit_price_ore": 100000}).json()["id"]
        bid = client.post(f"/books/{book}/stock", json={
            "article_id": aid, "qty_centi": 500, "unit_cost_ore": 60000}).json()["id"]
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "Widget", "quantity_centi": 200, "unit_price_ore": 100000,
                       "rate_code": "25", "article_id": aid, "stock_batch_id": bid}]}).json()
        assert client.get(f"/books/{book}/articles/{aid}/batches").json()[0]["qty_remaining_centi"] == 300
        # edit to consume 100 instead of 200 -> batch back to 400
        r = client.put(f"/books/{book}/invoices/{inv['invoice_id']}", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "Widget", "quantity_centi": 100, "unit_price_ore": 100000,
                       "rate_code": "25", "article_id": aid, "stock_batch_id": bid}]})
        assert r.status_code == 200
        assert client.get(f"/books/{book}/articles/{aid}/batches").json()[0]["qty_remaining_centi"] == 400

    def test_kreditera_paid(self, client, book):
        inv = self._inv(client, book)
        client.post(f"/books/{book}/invoices/{inv['invoice_id']}/pay", json={"date": "2026-03-10"})
        res = client.post(f"/books/{book}/invoices/{inv['invoice_id']}/credit",
                          json={"reason": "fel", "date": "2026-03-20"})
        assert res.status_code == 201
        assert client.get(f"/books/{book}/invoices").json()[0]["state"] == "credited"

    def test_partial_credit(self, client, book):
        inv = self._inv(client, book)   # inc 125000
        client.post(f"/books/{book}/invoices/{inv['invoice_id']}/pay", json={"date": "2026-03-10"})
        r = client.post(f"/books/{book}/invoices/{inv['invoice_id']}/credit",
                        json={"amount_ore": 25000, "reason": "retur", "date": "2026-03-20"})
        assert r.status_code == 201
        # paid 125000, credited 25000 -> owe the customer 25000 (negative outstanding)
        assert client.get(f"/books/{book}/invoices").json()[0]["outstanding_ore"] == -25000

    def test_credit_note_pdf(self, client, book):
        inv = self._inv(client, book)
        client.post(f"/books/{book}/invoices/{inv['invoice_id']}/pay", json={"date": "2026-03-10"})
        cr = client.post(f"/books/{book}/invoices/{inv['invoice_id']}/credit",
                         json={"reason": "fel", "date": "2026-03-20"}).json()
        assert cr["credit_note_number"] > inv["invoice_number"]      # unbroken series
        # the credit event is exposed on the invoice so the UI can find it
        ev = [e for e in client.get(f"/books/{book}/invoices/{inv['invoice_id']}").json()["events"]
              if e["kind"] == "credit"][0]
        assert ev["credit_note_number"] == cr["credit_note_number"]
        pdf = client.get(
            f"/books/{book}/invoices/{inv['invoice_id']}/credit-notes/{ev['id']}/pdf")
        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.content[:4] == b"%PDF"


class TestAccountingMethod:
    def test_get_set_method(self, client, book):
        assert client.get(f"/books/{book}/accounting-method").json()["method"] == "kontantmetod"
        assert client.put(f"/books/{book}/accounting-method",
                          json={"method": "fakturametod"}).status_code == 200
        assert client.get(f"/books/{book}/accounting-method").json()["method"] == "fakturametod"

    def test_fakturametod_books_invoice_at_issue(self, client, book):
        client.put(f"/books/{book}/accounting-method", json={"method": "fakturametod"})
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "T", "kind": "income", "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "private", "first_name": "A", "last_name": "B",
                                "personnummer": "811218-9876"}).json()["kundnummer"]
        client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "X", "quantity_centi": 100,
                       "unit_price_ore": 100000, "rate_code": "25"}]})
        # already booked at issue -> appears as a verifikation
        vers = client.get(f"/books/{book}/verifikationer").json()
        assert len(vers) == 1 and vers[0]["ver_date"] == "2026-03-01"

    def test_invalid_method_rejected(self, client, book):
        assert client.put(f"/books/{book}/accounting-method",
                          json={"method": "nope"}).status_code == 400


class TestArticles:
    def test_article_number_format_and_prefix(self, client, book):
        import re
        r = client.post(f"/books/{book}/articles", json={
            "description": "Konsulttimme", "prefix": "1000", "unit_price_ore": 120000,
            "rate_code": "25", "unit": "h"})
        assert r.status_code == 201
        num = r.json()["article_number"]
        assert re.match(r"^\d{4}-\d{4}$", num) and num.startswith("1000-")

    def test_bad_prefix_rejected(self, client, book):
        assert client.post(f"/books/{book}/articles",
                           json={"description": "X", "prefix": "12"}).status_code == 400

    def test_create_uncategorised_then_categorise_and_delete(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "IT", "kind": "income", "bas_konto": 3001}).json()["id"]
        aid = client.post(f"/books/{book}/articles",
                          json={"description": "Vara", "prefix": "2000"}).json()["id"]
        art = [a for a in client.get(f"/books/{book}/articles").json() if a["id"] == aid][0]
        assert art["category_id"] is None          # first uncategorised
        # categorise + reprice in the list
        client.patch(f"/books/{book}/articles/{aid}", json={"category_id": cat, "unit_price_ore": 5000})
        art = [a for a in client.get(f"/books/{book}/articles").json() if a["id"] == aid][0]
        assert art["category_id"] == cat and art["unit_price_ore"] == 5000
        assert client.delete(f"/books/{book}/articles/{aid}").status_code == 200
        assert all(a["id"] != aid for a in client.get(f"/books/{book}/articles").json())

    def test_invoice_line_links_article_and_survives_delete(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "IT", "kind": "income", "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "X AB"}).json()["kundnummer"]
        aid = client.post(f"/books/{book}/articles", json={
            "description": "Konsult", "prefix": "1000", "unit_price_ore": 100000,
            "category_id": cat}).json()["id"]
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "Konsult", "quantity_centi": 100,
                       "unit_price_ore": 90000, "rate_code": "25", "article_id": aid}]}).json()
        # price was edited (90000) and still booked; deleting the article keeps the invoice
        assert inv["ex_moms_ore"] == 90000
        assert client.delete(f"/books/{book}/articles/{aid}").status_code == 200
        assert client.get(f"/books/{book}/invoices/{inv['invoice_id']}").json()["ex_moms_ore"] == 90000


class TestExpenseDrafts:
    def test_expense_draft_crud(self, client, book):
        sup = client.post(f"/books/{book}/suppliers", json={"name": "Inet"}).json()["id"]
        payload = {"supplier_id": sup, "trans_date": "2026-05-01",
                   "items": [{"description": "Router", "quantity_centi": 100,
                              "unit_cost_ore": 60000, "rate_code": "25"}]}
        d = client.post(f"/books/{book}/expense-drafts", json={"payload": payload})
        assert d.status_code == 201
        did = d.json()["id"]
        lst = client.get(f"/books/{book}/expense-drafts").json()
        assert len(lst) == 1 and lst[0]["supplier_id"] == sup
        got = client.get(f"/books/{book}/expense-drafts/{did}").json()
        assert got["payload"]["items"][0]["description"] == "Router"
        assert client.delete(f"/books/{book}/expense-drafts/{did}").status_code == 200
        assert client.get(f"/books/{book}/expense-drafts").json() == []


class TestCustomerDelete:
    def test_delete_customer_keeps_invoice(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "Tjänst", "kind": "income", "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "Acme AB"}).json()["kundnummer"]
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "X", "quantity_centi": 100, "unit_price_ore": 10000,
                       "rate_code": "25"}]}).json()
        r = client.delete(f"/books/{book}/customers/{kid}")
        assert r.status_code == 200 and r.json()["deleted"] is True
        assert all(c["kundnummer"] != kid for c in client.get(f"/books/{book}/customers").json())
        # invoice kept with its frozen buyer, link detached
        got = client.get(f"/books/{book}/invoices/{inv['invoice_id']}").json()
        assert got["customer_id"] is None and got["buyer"]["company_name"] == "Acme AB"


class TestCompanyContacts:
    def _company_and_person(self, client, book):
        comp = client.post(f"/books/{book}/customers", json={
            "type": "business", "company_name": "Acme AB", "org_nr": "556000-0001",
            "vat_nr": "SE556000000101", "street": "Storg 1", "zip_code": "11122",
            "city": "Stockholm", "email": "a@acme.se"}).json()["kundnummer"]
        person = client.post(f"/books/{book}/customers", json={
            "type": "private", "first_name": "Anna", "last_name": "Andersson"}).json()["kundnummer"]
        return comp, person

    def test_link_list_unlink_contact(self, client, book):
        comp, person = self._company_and_person(client, book)
        r = client.post(f"/books/{book}/customers/{comp}/contacts",
                        json={"contact_kundnummer": person})
        assert r.status_code == 201
        lst = client.get(f"/books/{book}/customers/{comp}/contacts").json()
        assert [c["kundnummer"] for c in lst] == [person]
        client.delete(f"/books/{book}/customers/{comp}/contacts/{person}")
        assert client.get(f"/books/{book}/customers/{comp}/contacts").json() == []

    def test_contact_type_guard(self, client, book):
        comp, person = self._company_and_person(client, book)
        # a private customer cannot host contacts (company must be a business) -> 409
        r = client.post(f"/books/{book}/customers/{person}/contacts",
                        json={"contact_kundnummer": comp})
        assert r.status_code == 409

    def test_invoice_with_contact_freezes_name_and_company(self, client, book):
        comp, person = self._company_and_person(client, book)
        client.post(f"/books/{book}/customers/{comp}/contacts", json={"contact_kundnummer": person})
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "Tjänst", "kind": "income", "bas_konto": 3001}).json()["id"]
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": comp, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31", "contact_customer_id": person,
            "lines": [{"description": "Jobb", "quantity_centi": 100, "unit_price_ore": 100000,
                       "rate_code": "25"}]}).json()
        got = client.get(f"/books/{book}/invoices/{inv['invoice_id']}").json()
        assert got["contact_customer_id"] == person
        # buyer keeps the company details; the contact's name is frozen alongside
        assert got["buyer"]["company_name"] == "Acme AB"
        assert got["buyer"]["vat_nr"] == "SE556000000101"
        assert got["buyer"]["contact_person"] == "Anna Andersson"


class TestDeliveryAddress:
    def _setup(self, client, book):
        comp = client.post(f"/books/{book}/customers", json={
            "type": "business", "company_name": "Acme AB", "org_nr": "556000-0001",
            "vat_nr": "SE1", "street": "Storg 1", "zip_code": "11122",
            "city": "Stockholm", "email": "a@acme.se"}).json()["kundnummer"]
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "Tjänst", "kind": "income", "bas_konto": 3001}).json()["id"]
        return comp, cat

    def test_delivery_address_frozen_on_invoice(self, client, book):
        comp, cat = self._setup(client, book)
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": comp, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "delivery_address": {"name": "Acme Lager", "street": "Lagerv 9",
                                 "zip_code": "22233", "city": "Lund", "email": "l@acme.se"},
            "lines": [{"description": "X", "quantity_centi": 100, "unit_price_ore": 10000,
                       "rate_code": "25"}]}).json()
        got = client.get(f"/books/{book}/invoices/{inv['invoice_id']}").json()
        assert got["delivery_address"]["name"] == "Acme Lager"
        assert got["delivery_address"]["city"] == "Lund"

    def test_no_delivery_address_is_null(self, client, book):
        comp, cat = self._setup(client, book)
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": comp, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "X", "quantity_centi": 100, "unit_price_ore": 10000,
                       "rate_code": "25"}]}).json()
        got = client.get(f"/books/{book}/invoices/{inv['invoice_id']}").json()
        assert got["delivery_address"] is None            # PDF then uses the billing address

    def test_empty_delivery_normalises_to_null(self, client, book):
        comp, cat = self._setup(client, book)
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": comp, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31", "delivery_address": {"name": "", "city": "  "},
            "lines": [{"description": "X", "quantity_centi": 100, "unit_price_ore": 10000,
                       "rate_code": "25"}]}).json()
        got = client.get(f"/books/{book}/invoices/{inv['invoice_id']}").json()
        assert got["delivery_address"] is None


class TestStock:
    def _art(self, client, book):
        return client.post(f"/books/{book}/articles", json={
            "description": "Router", "prefix": "1000", "unit_price_ore": 100000}).json()["id"]

    def test_add_batch_and_list_stock(self, client, book):
        aid = self._art(client, book)
        r = client.post(f"/books/{book}/stock", json={
            "article_id": aid, "qty_centi": 500, "unit_cost_ore": 60000,
            "received_date": "2026-03-01"})
        assert r.status_code == 201 and r.json()["batch_number"] == 1
        stock = client.get(f"/books/{book}/stock").json()
        assert len(stock) == 1 and stock[0]["qty_remaining_centi"] == 500
        batches = client.get(f"/books/{book}/articles/{aid}/batches").json()
        assert batches[0]["unit_cost_ore"] == 60000

    def test_delete_article_with_stock_batches(self, client, book):
        # deleting an article that has stock batches used to 500 on the batch FK
        aid = self._art(client, book)
        client.post(f"/books/{book}/stock", json={
            "article_id": aid, "qty_centi": 500, "unit_cost_ore": 60000,
            "received_date": "2026-03-01"})
        r = client.delete(f"/books/{book}/articles/{aid}")
        assert r.status_code in (200, 204)
        assert client.get(f"/books/{book}/articles").json() == []
        assert client.get(f"/books/{book}/stock").json() == []      # its batches went too

    def test_invoice_picks_batch_and_reports_margin(self, client, book):
        cat = client.post(f"/books/{book}/categories",
                          json={"name": "Försäljning", "kind": "income", "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "X AB"}).json()["kundnummer"]
        aid = self._art(client, book)
        bid = client.post(f"/books/{book}/stock", json={
            "article_id": aid, "qty_centi": 500, "unit_cost_ore": 60000}).json()["id"]
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "Router", "quantity_centi": 200, "unit_price_ore": 100000,
                       "rate_code": "25", "article_id": aid, "stock_batch_id": bid}]}).json()
        got = client.get(f"/books/{book}/invoices/{inv['invoice_id']}").json()
        assert got["cost_ore"] == 120000 and got["margin_ore"] == 80000
        # stock consumed
        assert client.get(f"/books/{book}/articles/{aid}/batches").json()[0]["qty_remaining_centi"] == 300

    def test_delete_untouched_batch(self, client, book):
        aid = self._art(client, book)
        bid = client.post(f"/books/{book}/stock", json={
            "article_id": aid, "qty_centi": 500, "unit_cost_ore": 60000}).json()["id"]
        assert client.delete(f"/books/{book}/stock/{bid}").status_code == 200
        assert client.get(f"/books/{book}/stock").json() == []

    def test_inkop_items_create_articles_and_batches(self, client, book):
        prod = client.post(f"/books/{book}/categories",
                           json={"name": "Nätverk", "kind": "income", "bas_konto": 3001,
                                 "prefix": "0007"}).json()["id"]
        expcat = client.post(f"/books/{book}/categories",
                             json={"name": "Inköp varor", "kind": "expense", "bas_konto": 4010}).json()["id"]
        # An inköp with line-items: one stocked article + one pure cost line (no name).
        res = client.post(f"/books/{book}/expenses", json={
            "category_id": expcat, "trans_date": "2026-05-01", "paid_date": "2026-05-01",
            "items": [
                {"description": "Router X", "category_id": prod, "quantity_centi": 500,
                 "unit_cost_ore": 60000, "rate_code": "25"},
                {"description": "", "quantity_centi": 100, "unit_cost_ore": 5000, "rate_code": "25"},
            ]}).json()
        assert len(res["batches"]) == 1                       # only the named line stocked
        aid = res["batches"][0]["article_id"]
        art = [a for a in client.get(f"/books/{book}/articles").json() if a["id"] == aid][0]
        assert art["article_number"].startswith("0007-")      # article uses product prefix
        stock = client.get(f"/books/{book}/stock").json()
        assert stock[0]["qty_remaining_centi"] == 500
        # buying the same article again adds a 2nd batch to the SAME article
        res2 = client.post(f"/books/{book}/expenses", json={
            "category_id": expcat, "trans_date": "2026-06-01", "paid_date": "2026-06-01",
            "items": [{"description": "Router X", "category_id": prod, "quantity_centi": 300,
                       "unit_cost_ore": 65000, "rate_code": "25"}]}).json()
        assert res2["batches"][0]["article_id"] == aid
        assert res2["batches"][0]["batch_number"] == 2
        assert client.get(f"/books/{book}/stock").json()[0]["qty_remaining_centi"] == 800

    def test_subcategory_inherits_and_cycle_guard(self, client, book):
        parent = client.post(f"/books/{book}/categories",
                             json={"name": "Hårdvara", "kind": "income", "bas_konto": 3010,
                                   "default_rate_code": "25"}).json()["id"]
        # subcategory: omit kind + bas_konto -> inherited from parent
        sub = client.post(f"/books/{book}/categories",
                          json={"name": "Nätverk", "parent_id": parent}).json()["id"]
        cats = {c["id"]: c for c in client.get(f"/books/{book}/categories").json()}
        assert cats[sub]["parent_id"] == parent
        assert cats[sub]["kind"] == "income" and cats[sub]["bas_konto"] == 3010
        # reparent the parent under its own child -> cycle -> 409
        r = client.patch(f"/books/{book}/categories/{parent}", json={"parent_id": sub})
        assert r.status_code == 409

    def test_next_prefix_and_duplicate_rejected(self, client, book):
        p = client.get(f"/books/{book}/categories/next-prefix").json()["prefix"]
        assert p == "0000"
        client.post(f"/books/{book}/categories",
                    json={"name": "A", "kind": "income", "bas_konto": 3001, "prefix": "0500"})
        dup = client.post(f"/books/{book}/categories",
                          json={"name": "B", "kind": "income", "bas_konto": 3002, "prefix": "0500"})
        assert dup.status_code == 409          # prefix in use -> InvalidState

    def test_stock_writeoff_logs_reason_and_lowers_qty(self, client, book):
        aid = self._art(client, book)
        bid = client.post(f"/books/{book}/stock", json={
            "article_id": aid, "qty_centi": 500, "unit_cost_ore": 60000}).json()["id"]
        # write off 2 units (200 centi) — broken during testing
        r = client.post(f"/books/{book}/stock/{bid}/adjust",
                        json={"qty_delta_centi": -200, "reason": "Trasig vid test"})
        assert r.status_code == 201 and r.json()["qty_remaining_centi"] == 300
        assert client.get(f"/books/{book}/stock").json()[0]["qty_remaining_centi"] == 300
        log = client.get(f"/books/{book}/stock/{bid}/adjustments").json()
        assert len(log) == 1 and log[0]["reason"] == "Trasig vid test"
        assert log[0]["qty_delta_centi"] == -200

    def test_stock_writeoff_cannot_exceed_remaining_or_be_positive(self, client, book):
        aid = self._art(client, book)
        bid = client.post(f"/books/{book}/stock", json={
            "article_id": aid, "qty_centi": 100, "unit_cost_ore": 60000}).json()["id"]
        assert client.post(f"/books/{book}/stock/{bid}/adjust",
                           json={"qty_delta_centi": -200, "reason": "för mycket"}).status_code == 409
        assert client.post(f"/books/{book}/stock/{bid}/adjust",
                           json={"qty_delta_centi": 50, "reason": "ökning"}).status_code == 400
        assert client.post(f"/books/{book}/stock/{bid}/adjust",
                           json={"qty_delta_centi": -50, "reason": ""}).status_code == 400


class TestInkopEditDelete:
    def _expcat(self, client, book):
        return client.post(f"/books/{book}/categories",
                           json={"name": "Förbrukning", "kind": "expense",
                                 "bas_konto": 5460}).json()["id"]

    def _pending(self, client, book, cat, amount=10000):
        return client.post(f"/books/{book}/expenses",
                           json={"category_id": cat, "trans_date": "2026-03-01",
                                 "lines": [{"rate_code": "25", "amount_ore": amount,
                                            "inclusive": True}]}).json()["transaktion_id"]

    def _row(self, client, book, tid, **kw):
        rows = client.get(f"/books/{book}/transaktioner", params=kw).json()
        return next((t for t in rows if t["id"] == tid), None)

    def test_full_edit_of_unbooked_inkop(self, client, book):
        cat = self._expcat(client, book)
        other = client.post(f"/books/{book}/categories",
                            json={"name": "Kontor", "kind": "expense", "bas_konto": 6110}).json()["id"]
        tid = self._pending(client, book, cat)
        # full edit: change konto, belopp, moms, date, öresavrundning
        r = client.patch(f"/books/{book}/transaktioner/{tid}", json={
            "category_id": other, "trans_date": "2026-04-02",
            "lines": [{"rate_code": "6", "amount_ore": 21200, "inclusive": True}],
            "ores_rounding": True, "ext_ref": "NY-REF"})
        assert r.status_code == 200
        row = self._row(client, book, tid)
        assert row["category_id"] == other and row["amount_ore"] == 21200
        assert row["trans_date"] == "2026-04-02" and row["ext_ref"] == "NY-REF"
        assert row["ores_rounding"] == 1
        # the moms line was rebuilt at 6 %
        mls = client.get(f"/books/{book}/transaktioner/{tid}/lines").json()
        assert len(mls) == 1 and mls[0]["rate_code"] == "6"

    def test_full_edit_refused_after_booked(self, client, book):
        cat = self._expcat(client, book)
        tid = self._pending(client, book, cat)
        client.post(f"/books/{book}/transaktioner/{tid}/pay", json={"payment_date": "2026-03-05"})
        r = client.patch(f"/books/{book}/transaktioner/{tid}", json={
            "category_id": cat, "trans_date": "2026-03-01",
            "lines": [{"rate_code": "25", "amount_ore": 50000, "inclusive": True}]})
        assert r.status_code == 409          # booked -> immutable, use rättelse

    def test_ores_toggle_refused_after_booked(self, client, book):
        cat = self._expcat(client, book)
        tid = self._pending(client, book, cat)
        client.post(f"/books/{book}/transaktioner/{tid}/pay", json={"payment_date": "2026-03-05"})
        assert client.patch(f"/books/{book}/transaktioner/{tid}",
                            json={"ores_rounding": True}).status_code == 409

    def test_soft_delete_and_restore_pending(self, client, book):
        cat = self._expcat(client, book)
        tid = self._pending(client, book, cat)
        assert client.post(f"/books/{book}/transaktioner/{tid}/delete").status_code == 200
        assert self._row(client, book, tid) is None                    # hidden from normal list
        deleted = self._row(client, book, tid, only_deleted=True)
        assert deleted is not None and deleted["deleted"] == 1          # shows in Borttagna
        assert client.post(f"/books/{book}/transaktioner/{tid}/restore").status_code == 200
        assert self._row(client, book, tid) is not None                # back in the normal list

    def test_soft_delete_refused_after_booked(self, client, book):
        cat = self._expcat(client, book)
        tid = self._pending(client, book, cat)
        client.post(f"/books/{book}/transaktioner/{tid}/pay", json={"payment_date": "2026-03-05"})
        assert client.post(f"/books/{book}/transaktioner/{tid}/delete").status_code == 409

    def test_soft_delete_hides_stock_then_restores(self, client, book):
        prod = client.post(f"/books/{book}/categories",
                           json={"name": "Nät", "kind": "income", "bas_konto": 3001,
                                 "prefix": "0300"}).json()["id"]
        cat = self._expcat(client, book)
        res = client.post(f"/books/{book}/expenses", json={
            "category_id": cat, "trans_date": "2026-05-01",
            "items": [{"description": "Kabel", "category_id": prod, "quantity_centi": 500,
                       "unit_cost_ore": 6000, "rate_code": "25"}]}).json()
        tid = res["transaktion_id"]
        assert client.get(f"/books/{book}/stock").json()[0]["qty_remaining_centi"] == 500
        client.post(f"/books/{book}/transaktioner/{tid}/delete")
        assert client.get(f"/books/{book}/stock").json() == []          # stock hidden with it
        client.post(f"/books/{book}/transaktioner/{tid}/restore")
        assert client.get(f"/books/{book}/stock").json()[0]["qty_remaining_centi"] == 500

    def test_edit_payload_reconstructs_items_and_full_edit_rebuilds_stock(self, client, book):
        prod = client.post(f"/books/{book}/categories",
                           json={"name": "Nät", "kind": "income", "bas_konto": 3001,
                                 "prefix": "0600"}).json()["id"]
        cat = self._expcat(client, book)
        res = client.post(f"/books/{book}/expenses", json={
            "category_id": cat, "trans_date": "2026-05-01",
            "items": [{"description": "Kabel", "category_id": prod, "quantity_centi": 500,
                       "unit_cost_ore": 6000, "rate_code": "25"},
                      {"description": "", "quantity_centi": 100, "unit_cost_ore": 4000,
                       "rate_code": "25"}]}).json()
        tid = res["transaktion_id"]
        pf = client.get(f"/books/{book}/transaktioner/{tid}/edit-payload").json()
        assert pf["booked"] is False and pf["category_id"] == cat
        stocked = [i for i in pf["items"] if i["description"]]
        assert stocked and stocked[0]["description"] == "Kabel"
        assert stocked[0]["quantity_centi"] == 500 and stocked[0]["unit_cost_ore"] == 6000
        # edit: bump the cable qty to 800 — the batch is rebuilt
        newitems = [dict(i) for i in pf["items"]]
        for i in newitems:
            if i["description"] == "Kabel":
                i["quantity_centi"] = 800
        r = client.patch(f"/books/{book}/transaktioner/{tid}", json={
            "category_id": cat, "trans_date": pf["trans_date"], "items": newitems})
        assert r.status_code == 200
        assert client.get(f"/books/{book}/stock").json()[0]["qty_remaining_centi"] == 800
        # and it still books cleanly when paid
        pay = client.post(f"/books/{book}/transaktioner/{tid}/pay",
                          json={"payment_date": "2026-05-10"})
        assert pay.status_code == 200

    def test_delete_refused_when_stock_consumed(self, client, book):
        prod = client.post(f"/books/{book}/categories",
                           json={"name": "Nät", "kind": "income", "bas_konto": 3001,
                                 "prefix": "0400"}).json()["id"]
        icat = client.post(f"/books/{book}/categories",
                           json={"name": "Sälj", "kind": "income", "bas_konto": 3002}).json()["id"]
        cat = self._expcat(client, book)
        res = client.post(f"/books/{book}/expenses", json={
            "category_id": cat, "trans_date": "2026-05-01",
            "items": [{"description": "Switch", "category_id": prod, "quantity_centi": 500,
                       "unit_cost_ore": 6000, "rate_code": "25"}]}).json()
        tid = res["transaktion_id"]
        aid = res["batches"][0]["article_id"]
        bid = res["batches"][0]["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "Y AB"}).json()["kundnummer"]
        client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": icat, "invoice_date": "2026-05-02",
            "due_date": "2026-05-30",
            "lines": [{"description": "Switch", "quantity_centi": 100, "unit_price_ore": 10000,
                       "rate_code": "25", "article_id": aid, "stock_batch_id": bid}]})
        # goods sold -> the inköp can no longer be discarded
        assert client.post(f"/books/{book}/transaktioner/{tid}/delete").status_code == 409


class TestBasKontonAndAddress:
    def test_accounts_endpoint_lists_system_konton(self, client, book):
        client.post(f"/books/{book}/categories",
                    json={"name": "IT 25%", "kind": "income", "bas_konto": 3001,
                          "default_rate_code": "25"})
        accts = client.get(f"/books/{book}/accounts").json()
        by = {a["bas_konto"]: a for a in accts}
        assert by[1930]["is_system"] and by[1930]["system_label"]
        assert by[3001]["is_system"] is False and by[3001]["category_count"] == 1

    def test_delete_unused_category(self, client, book):
        cid = client.post(f"/books/{book}/categories",
                          json={"name": "Oanvänd", "kind": "income", "bas_konto": 3999}).json()["id"]
        # list flags it as not used
        cats = client.get(f"/books/{book}/categories").json()
        assert [c for c in cats if c["id"] == cid][0]["used"] == 0
        resp = client.delete(f"/books/{book}/categories/{cid}")
        assert resp.status_code == 200 and resp.json()["deleted"] is True
        assert all(c["id"] != cid for c in client.get(f"/books/{book}/categories").json())

    def test_delete_used_category_409(self, client, book):
        cid = client.post(f"/books/{book}/categories",
                          json={"name": "Använd", "kind": "income", "bas_konto": 3001}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "X AB"}).json()["kundnummer"]
        client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cid, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "A", "quantity_centi": 100,
                       "unit_price_ore": 100000, "rate_code": "25"}]})
        assert [c for c in client.get(f"/books/{book}/categories").json()
                if c["id"] == cid][0]["used"] == 1
        assert client.delete(f"/books/{book}/categories/{cid}").status_code == 409

    def test_category_carries_default_rate(self, client, book):
        client.post(f"/books/{book}/categories",
                    json={"name": "Varor 12%", "kind": "income", "bas_konto": 3002,
                          "default_rate_code": "12"})
        cats = client.get(f"/books/{book}/categories").json()
        assert [c for c in cats if c["bas_konto"] == 3002][0]["default_rate_code"] == "12"

    def test_customer_structured_address(self, client, book):
        kid = client.post(f"/books/{book}/customers", json={
            "type": "business", "company_name": "Köpare AB", "street": "Storgatan 1",
            "zip_code": "11122", "city": "Stockholm"}).json()["kundnummer"]
        c = client.get(f"/books/{book}/customers/{kid}").json()
        assert c["country"] == "Sverige"
        assert c["city"] == "Stockholm" and "Storgatan 1" in c["address"]

    def test_invoice_per_line_categories_split_booking(self, client, book):
        it = client.post(f"/books/{book}/categories",
                         json={"name": "IT", "kind": "income", "bas_konto": 3001}).json()["id"]
        varor = client.post(f"/books/{book}/categories",
                            json={"name": "Varor", "kind": "income", "bas_konto": 3002}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "X AB"}).json()["kundnummer"]
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "invoice_date": "2026-03-01", "due_date": "2026-03-31",
            "lines": [{"description": "Tjänst", "quantity_centi": 100, "unit_price_ore": 100000,
                       "rate_code": "25", "category_id": it},
                      {"description": "Vara", "quantity_centi": 100, "unit_price_ore": 40000,
                       "rate_code": "25", "category_id": varor}]}).json()
        assert inv["ex_moms_ore"] == 140000
        got = client.get(f"/books/{book}/invoices/{inv['invoice_id']}").json()
        cats = {ln["category_id"] for ln in got["lines"]}
        assert cats == {it, varor}


# ---------------------------------------------------------------------------
# Server mode (Phase 1): API token gate + server-side book placement
# ---------------------------------------------------------------------------

class TestServerMode:
    def _server(self, tmp_path):
        from backend.api import create_app
        data = tmp_path / "srv"
        return create_app(app_dir=data / "app", books_dir=data, api_token="SECRET",
                          cors_origins=["*"], autolock_seconds=900), data

    def test_token_gate(self, tmp_path):
        app, _ = self._server(tmp_path)
        with TestClient(app) as c:
            assert c.get("/").status_code == 200                    # health open
            assert c.get("/books").status_code == 401               # API needs token
            assert c.get("/books", headers={"Authorization": "Bearer NOPE"}).status_code == 401
            assert c.get("/books", headers={"Authorization": "Bearer SECRET"}).status_code == 200

    def test_books_placed_under_server_dir(self, tmp_path):
        app, data = self._server(tmp_path)
        H = {"Authorization": "Bearer SECRET"}
        with TestClient(app) as c:
            r = c.post("/books", headers=H, json={"display_name": "Min Firma",
                       "db_path": "/client/ignored/path.db", "passphrase": "pw"})
            assert r.status_code == 201
            dbp = r.json()["db_path"]
            assert dbp.startswith(str((data / "books").resolve()))   # placed on the server
            assert "ignored" not in dbp                              # client path ignored

    def test_local_mode_unchanged(self, tmp_path):
        from backend.api import create_app
        app = create_app(app_dir=tmp_path / "local", autolock_seconds=900)
        with TestClient(app) as c:
            assert c.get("/books").status_code == 200                # no token needed locally

    def test_read_token_from_env_or_file(self, tmp_path):
        from backend.server import read_token
        assert read_token({"BOKYUP_API_TOKEN": "  abc  "}) == "abc"   # env wins, trimmed
        tf = tmp_path / "token"
        tf.write_text("filesecret\n")
        assert read_token({"BOKYUP_API_TOKEN_FILE": str(tf)}) == "filesecret"
        # env takes priority over the file
        assert read_token({"BOKYUP_API_TOKEN": "envtok", "BOKYUP_API_TOKEN_FILE": str(tf)}) == "envtok"
        assert read_token({}) == ""                                  # nothing set -> refuse


class TestBasCatalogAndKontoEdit:
    """Preset BAS-konton + editing a konto that has already been booked on."""

    def test_catalog_lists_and_adds_categories_and_balance_konton(self, client, book):
        cat = client.get(f"/books/{book}/bas-katalog").json()
        by = {e["bas_konto"]: e for e in cat}
        assert by[3041]["kind"] == "income" and by[3041]["rate_code"] == "25"
        assert by[5410]["kind"] == "expense"
        assert by[2018]["kind"] == "equity"          # balanskonto, not a category
        # system konton are created lazily, so a fresh book has not got 1930 in the
        # chart yet; adding it from the catalog is idempotent either way
        assert by[1930]["added"] is False

        r = client.post(f"/books/{book}/bas-katalog/add",
                        json={"konton": [3041, 5410, 2018]})
        assert r.status_code == 201
        created = {c["bas_konto"]: c for c in r.json()["created"]}
        assert created[3041]["category_id"] and created[5410]["category_id"]
        assert created[2018]["category_id"] is None  # chart only

        cats = {c["bas_konto"]: c for c in client.get(f"/books/{book}/categories").json()}
        assert cats[3041]["kind"] == "income" and cats[3041]["default_rate_code"] == "25"
        assert 2018 not in cats
        assert 2018 in {a["bas_konto"] for a in client.get(f"/books/{book}/accounts").json()}

        # re-running is safe: already-present konton are skipped, not duplicated
        again = client.post(f"/books/{book}/bas-katalog/add",
                            json={"konton": [3041, 2018]}).json()
        assert again["created"] == [] and set(again["skipped"]) == {3041, 2018}
        assert client.post(f"/books/{book}/bas-katalog/add",
                           json={"konton": [9999]}).status_code == 400

    def test_editing_a_used_kontos_bas_number_does_not_rewrite_history(self, client, book):
        cid = client.post(f"/books/{book}/categories",
                          json={"name": "Förbrukning", "kind": "expense",
                                "bas_konto": 5410}).json()["id"]
        client.post(f"/books/{book}/expenses",
                    json={"category_id": cid,
                          "lines": [{"rate_code": "25", "amount_ore": 1250}],
                          "trans_date": "2026-02-01", "paid_date": "2026-02-01"})

        # the konto is in use, and may STILL be edited
        assert [c for c in client.get(f"/books/{book}/categories").json()
                if c["id"] == cid][0]["used"] == 1
        assert client.patch(f"/books/{book}/categories/{cid}",
                            json={"bas_konto": 5460}).status_code == 200

        # history is untouched: huvudbok (postings) and the result report both keep the
        # already-booked cost on 5410 — the new konto only applies from the next booking.
        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb.get(5410) == 1000 and 5460 not in hb
        res = client.get(f"/books/{book}/reports/result",
                         params={"start": "2026-01-01", "end": "2026-12-31"}).json()
        konton = {r["bas_konto"]: r["amount_ore"] for r in res["by_category"]}
        assert konton.get(5410) == 1000 and 5460 not in konton

        # a NEW purchase on the same category books to the new konto
        client.post(f"/books/{book}/expenses",
                    json={"category_id": cid,
                          "lines": [{"rate_code": "25", "amount_ore": 2500}],
                          "trans_date": "2026-03-01", "paid_date": "2026-03-01"})
        res2 = client.get(f"/books/{book}/reports/result",
                          params={"start": "2026-01-01", "end": "2026-12-31"}).json()
        konton2 = {r["bas_konto"]: r["amount_ore"] for r in res2["by_category"]}
        assert konton2.get(5410) == 1000 and konton2.get(5460) == 2000


class TestReverseCharge:
    """Omvänd betalningsskyldighet: the buyer reports both sides of the moms."""

    def _konto(self, client, book, name, kind, bas):
        return client.post(f"/books/{book}/categories",
                           json={"name": name, "kind": kind, "bas_konto": bas}).json()["id"]

    def test_eu_service_books_both_sides_and_nets_to_zero(self, client, book):
        cid = self._konto(client, book, "Programvaror", "expense", 5420)
        # An Anthropic-style EU service receipt: 420,75 kr, no moms charged by the seller.
        res = client.post(f"/books/{book}/expenses", json={
            "category_id": cid, "trans_date": "2026-06-30", "paid_date": "2026-06-30",
            "lines": [{"rate_code": "25", "amount_ore": 42075, "inclusive": False,
                       "reverse_charge": "eu_tjanst"}]}).json()
        assert "verifikation_id" in res

        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[5420] == 42075          # cost = the full ex-moms amount
        assert hb[2645] == 10519          # beräknad ingående moms (25 %)
        assert hb[2614] == -10519         # utgående moms omvänd skattskyldighet
        assert hb[1930] == -42075         # only the ex-moms amount actually leaves the bank
        assert 2640 not in hb             # normal ingående moms is untouched
        assert sum(hb.values()) == 0      # the verifikation balances

    def test_momsdeklaration_boxes_21_30_48(self, client, book):
        cid = self._konto(client, book, "IT-tjänster", "expense", 6540)
        client.post(f"/books/{book}/expenses", json={
            "category_id": cid, "trans_date": "2026-06-30", "paid_date": "2026-06-30",
            "lines": [{"rate_code": "25", "amount_ore": 42075, "inclusive": False,
                       "reverse_charge": "eu_tjanst"}]})
        rep = client.get(f"/books/{book}/reports/momsdeklaration",
                         params={"start": "2026-04-01", "end": "2026-06-30"}).json()
        b = rep["boxes"]
        assert b["21"] == 42075           # underlag: EU-tjänst enligt huvudregeln
        assert b["30"] == 10519           # utgående moms 25 % på förvärvet
        assert b["48"] == 10519           # samma belopp som ingående moms
        assert b["49"] == 0               # nets to zero with full avdragsrätt
        assert b["05"] == 0               # never counted as sales

    def test_each_kind_lands_in_its_own_box(self, client, book):
        cid = self._konto(client, book, "Varor", "expense", 4010)
        for kind, box in [("eu_vara", "20"), ("utanfor_eu", "22"),
                          ("sv_vara", "23"), ("sv_tjanst", "24")]:
            client.post(f"/books/{book}/expenses", json={
                "category_id": cid, "trans_date": "2026-05-01", "paid_date": "2026-05-01",
                "lines": [{"rate_code": "25", "amount_ore": 10000, "inclusive": False,
                           "reverse_charge": kind}]})
        b = client.get(f"/books/{book}/reports/momsdeklaration",
                       params={"start": "2026-05-01", "end": "2026-05-31"}).json()["boxes"]
        assert b["20"] == b["22"] == b["23"] == b["24"] == 10000
        assert b["30"] == 10000           # 4 × 2500 öre utgående moms

    def test_reverse_charge_rejected_on_momsfri_rate(self, client, book):
        cid = self._konto(client, book, "Övrigt", "expense", 6991)
        r = client.post(f"/books/{book}/expenses", json={
            "category_id": cid, "trans_date": "2026-05-01",
            "lines": [{"rate_code": "momsfri", "amount_ore": 10000,
                       "reverse_charge": "eu_tjanst"}]})
        assert r.status_code == 400 and "moms" in r.json()["detail"].lower()
        assert client.post(f"/books/{book}/expenses", json={
            "category_id": cid, "trans_date": "2026-05-01",
            "lines": [{"rate_code": "25", "amount_ore": 10000,
                       "reverse_charge": "nonsens"}]}).status_code == 400

    def test_kinds_endpoint(self, client, book):
        k = client.get(f"/books/{book}/reverse-charge-kinds").json()
        assert {x["value"]: x["box"] for x in k["kinds"]}["eu_tjanst"] == "21"
        assert k["rates"] == ["25", "12", "6"]


class TestRecurring:
    """Återkommande betalningar: confirm each occurrence; edits hit the series forward."""

    def _series(self, client, book, **over):
        cid = client.post(f"/books/{book}/categories",
                          json={"name": "Programvaror", "kind": "expense",
                                "bas_konto": 5420}).json()["id"]
        body = {"kind": "expense", "name": "Molnabonnemang", "category_id": cid,
                "start_date": "2026-01-15", "interval_unit": "month", "interval_count": 1,
                "lines": [{"rate_code": "25", "amount_ore": 12500}]}
        body.update(over)
        return cid, client.post(f"/books/{book}/recurring", json=body)

    def test_create_and_confirm_advances_the_series(self, client, book):
        _, r = self._series(client, book)
        assert r.status_code == 201
        rid = r.json()["id"]

        rec = client.get(f"/books/{book}/recurring").json()[0]
        assert rec["next_date"] == "2026-01-15" and rec["total_ore"] == 12500
        assert rec["booked_count"] == 0

        due = client.get(f"/books/{book}/recurring/due",
                         params={"as_of": "2026-01-20"}).json()
        assert [d["id"] for d in due] == [rid]

        res = client.post(f"/books/{book}/recurring/{rid}/confirm",
                          json={"paid_date": "2026-01-15"})
        assert res.status_code == 201 and res.json()["due_date"] == "2026-01-15"

        rec = client.get(f"/books/{book}/recurring").json()[0]
        assert rec["next_date"] == "2026-02-15" and rec["booked_count"] == 1
        # the confirmed occurrence is a perfectly ordinary booked transaktion
        tx = client.get(f"/books/{book}/transaktioner").json()
        assert len(tx) == 1 and "Molnabonnemang (2026-01-15)" in tx[0]["note"]
        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[5420] == 10000 and hb[2640] == 2500 and hb[1930] == -12500

    def test_confirming_twice_is_refused(self, client, book):
        _, r = self._series(client, book)
        rid = r.json()["id"]
        client.post(f"/books/{book}/recurring/{rid}/confirm", json={"paid_date": "2026-01-15"})
        again = client.post(f"/books/{book}/recurring/{rid}/confirm",
                            json={"date": "2026-01-15", "paid_date": "2026-01-15"})
        assert again.status_code == 409

    def test_edit_changes_the_whole_series_forward_but_not_history(self, client, book):
        _, r = self._series(client, book)
        rid = r.json()["id"]
        client.post(f"/books/{book}/recurring/{rid}/confirm", json={"paid_date": "2026-01-15"})

        # raise the price: every future occurrence follows, the booked one does not
        assert client.patch(f"/books/{book}/recurring/{rid}", json={
            "lines": [{"rate_code": "25", "amount_ore": 25000}]}).status_code == 200
        client.post(f"/books/{book}/recurring/{rid}/confirm", json={"paid_date": "2026-02-15"})

        amounts = sorted(t["amount_ore"] for t in client.get(f"/books/{book}/transaktioner").json())
        assert amounts == [12500, 25000]          # january untouched, february at the new price

    def test_one_off_amount_override_leaves_the_template_alone(self, client, book):
        _, r = self._series(client, book)
        rid = r.json()["id"]
        client.post(f"/books/{book}/recurring/{rid}/confirm",
                    json={"paid_date": "2026-01-15",
                          "lines": [{"rate_code": "25", "amount_ore": 40000}]})
        assert client.get(f"/books/{book}/transaktioner").json()[0]["amount_ore"] == 40000
        assert client.get(f"/books/{book}/recurring").json()[0]["total_ore"] == 12500

    def test_skip_advances_without_booking(self, client, book):
        _, r = self._series(client, book)
        rid = r.json()["id"]
        assert client.post(f"/books/{book}/recurring/{rid}/skip", json={}).status_code == 201
        assert client.get(f"/books/{book}/transaktioner").json() == []
        assert client.get(f"/books/{book}/recurring").json()[0]["next_date"] == "2026-02-15"
        hist = client.get(f"/books/{book}/recurring/{rid}/history").json()
        assert len(hist) == 1 and hist[0]["status"] == "skipped"

    def test_yearly_interval_and_end_date_closes_the_series(self, client, book):
        _, r = self._series(client, book, interval_unit="year", end_date="2026-12-31")
        rid = r.json()["id"]
        client.post(f"/books/{book}/recurring/{rid}/confirm", json={"paid_date": "2026-01-15"})
        rec = client.get(f"/books/{book}/recurring").json()[0]
        assert rec["next_date"] == "2027-01-15" and rec["active"] == 0

    def test_delete_refused_once_booked_but_allowed_while_untouched(self, client, book):
        _, r = self._series(client, book)
        rid = r.json()["id"]
        assert client.delete(f"/books/{book}/recurring/{rid}").status_code == 200
        _, r2 = self._series(client, book, name="Annat")
        rid2 = r2.json()["id"]
        client.post(f"/books/{book}/recurring/{rid2}/confirm", json={"paid_date": "2026-01-15"})
        assert client.delete(f"/books/{book}/recurring/{rid2}").status_code == 409

    def test_recurring_carries_reverse_charge_into_the_booking(self, client, book):
        _, r = self._series(client, book, name="Claude", lines=[
            {"rate_code": "25", "amount_ore": 42075, "reverse_charge": "eu_tjanst"}])
        rid = r.json()["id"]
        client.post(f"/books/{book}/recurring/{rid}/confirm", json={"paid_date": "2026-01-15"})
        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[2645] == 10519 and hb[2614] == -10519 and hb[1930] == -42075

    def test_bad_template_is_rejected_at_save_time(self, client, book):
        cid = client.post(f"/books/{book}/categories",
                          json={"name": "K", "kind": "expense", "bas_konto": 5420}).json()["id"]
        bad = {"kind": "expense", "name": "X", "category_id": cid, "start_date": "2026-01-15",
               "lines": [{"rate_code": "25", "amount_ore": 0}]}
        assert client.post(f"/books/{book}/recurring", json=bad).status_code == 400
        bad["lines"] = [{"rate_code": "25", "amount_ore": 100}]
        bad["interval_unit"] = "vecka"
        assert client.post(f"/books/{book}/recurring", json=bad).status_code == 400


class TestPrivateAssetContribution:
    """Privat tillgång in i verksamheten (tillskott) — debited against 2018, no moms."""

    def test_books_the_users_computer_case(self, client, book):
        # Bought privately in December, taken into full business use 1 January.
        res = client.post(f"/books/{book}/private-asset", json={
            "description": "MacBook Pro 14, serienr ABC123",
            "amount_ore": 1850000, "date": "2026-01-01",
            "acquired_date": "2025-12-18", "acquired_amount_ore": 1850000})
        assert res.status_code == 201
        out = res.json()
        assert out["treatment"] == "direktavdrag" and out["bas_konto"] == 5410

        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[5410] == 1850000 and hb[2018] == -1850000
        assert sum(hb.values()) == 0

        ver = client.get(f"/books/{book}/verifikationer-full").json()[0]
        assert ver["ver_date"] == "2026-01-01"
        assert "MacBook Pro 14" in ver["text"]
        # It is an egenupprättad verifikation and the motivation IS the underlag.
        assert ver["egenupprattad"] == 1
        for must in ("2025-12-18", "2026-01-01", "18 500,00 kr", "uteslutande"):
            assert must in ver["motivering"], ver["motivering"]

        # No moms anywhere — the private acquisition carried no avdragsrätt.
        boxes = client.get(f"/books/{book}/reports/momsdeklaration",
                           params={"start": "2026-01-01", "end": "2026-03-31"}).json()["boxes"]
        assert boxes["48"] == 0 and boxes["49"] == 0

    def test_over_half_a_prisbasbelopp_is_capitalised(self, client, book):
        # 2026 prisbasbelopp 59 200 -> threshold 29 600 kr.
        prev = client.get(f"/books/{book}/private-asset/preview",
                          params={"amount_ore": 4000000}).json()
        assert prev["threshold_ore"] == 2960000
        assert prev["suggested"] == "aktivera" and prev["bas_konto"] == 1220
        assert any("skrivas av" in n for n in prev["notes"])

        client.post(f"/books/{book}/private-asset", json={
            "description": "Serverskåp", "amount_ore": 4000000, "date": "2026-02-01"})
        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[1220] == 4000000 and hb[2018] == -4000000

    def test_mode_overrides_the_suggestion(self, client, book):
        out = client.post(f"/books/{book}/private-asset", json={
            "description": "Skrivare", "amount_ore": 500000, "date": "2026-02-01",
            "mode": "aktivera"}).json()
        assert out["treatment"] == "aktivera" and out["bas_konto"] == 1220
        assert any("direktavdrag hade" in n for n in out["notes"])

    def test_partial_business_use_books_only_that_share(self, client, book):
        out = client.post(f"/books/{book}/private-asset", json={
            "description": "Kamera", "amount_ore": 1000000, "date": "2026-03-01",
            "business_pct_centi": 6000}).json()
        assert out["booked_ore"] == 600000
        assert "60 %" in out["motivering"]
        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[5410] == 600000 and hb[2018] == -600000

    def test_own_motivation_is_kept_and_konto_can_be_chosen(self, client, book):
        out = client.post(f"/books/{book}/private-asset", json={
            "description": "Dator", "amount_ore": 900000, "date": "2026-01-01",
            "konto": 1250, "motivering": "Egen text om värderingen"}).json()
        assert out["bas_konto"] == 1250 and out["motivering"] == "Egen text om värderingen"

    def test_validation(self, client, book):
        base = {"description": "X", "amount_ore": 100000, "date": "2026-01-01"}
        assert client.post(f"/books/{book}/private-asset",
                           json={**base, "description": "  "}).status_code == 400
        assert client.post(f"/books/{book}/private-asset",
                           json={**base, "amount_ore": 0}).status_code == 400
        assert client.post(f"/books/{book}/private-asset",
                           json={**base, "business_pct_centi": 0}).status_code == 400
        assert client.post(f"/books/{book}/private-asset",
                           json={**base, "mode": "hittepa"}).status_code == 400

    def test_period_lock_is_respected(self, client, book):
        client.post(f"/books/{book}/period-locks",
                    json={"period_start": "2026-01-01", "period_end": "2026-03-31"})
        assert client.post(f"/books/{book}/private-asset", json={
            "description": "Dator", "amount_ore": 900000,
            "date": "2026-02-01"}).status_code == 409

    def test_manual_verifikation_can_be_marked_egenupprattad(self, client, book):
        body = {"ver_date": "2026-01-01", "text": "Eget uttag",
                "postings": [{"bas_konto": 2013, "debit_ore": 50000, "credit_ore": 0},
                             {"bas_konto": 1930, "debit_ore": 0, "credit_ore": 50000}],
                "egenupprattad": True}
        # the flag without a motivation is refused — the motivation IS the underlag
        assert client.post(f"/books/{book}/verifikationer/manual", json=body).status_code == 400
        body["motivering"] = "Privat uttag ur kassan, styrkt av kontoutdrag."
        assert client.post(f"/books/{book}/verifikationer/manual", json=body).status_code == 201
        ver = client.get(f"/books/{book}/verifikationer-full").json()[0]
        assert ver["egenupprattad"] == 1 and "kontoutdrag" in ver["motivering"]


class TestManualVerifikationRefAndComment:
    """A manual entry carries the same kvitto-/fakturanummer an inköp does, plus a comment."""

    def _post(self, client, book, **over):
        body = {"ver_date": "2026-03-01", "text": "Omföring materialkostnad",
                "postings": [{"bas_konto": 5460, "debit_ore": 50000, "credit_ore": 0},
                             {"bas_konto": 1930, "debit_ore": 0, "credit_ore": 50000}]}
        body.update(over)
        return client.post(f"/books/{book}/verifikationer/manual", json=body)

    def test_ref_and_comment_are_stored_and_shown_in_grundboken(self, client, book):
        assert self._post(client, book, ext_ref="INV-2026-07",
                          kommentar="Delbetalning enligt överenskommelse").status_code == 201
        ver = client.get(f"/books/{book}/verifikationer-full").json()[0]
        assert ver["ext_ref"] == "INV-2026-07"
        assert ver["kommentar"] == "Delbetalning enligt överenskommelse"

    def test_both_are_optional_and_blanks_become_null(self, client, book):
        assert self._post(client, book).status_code == 201
        assert self._post(client, book, ver_date="2026-03-02", ext_ref="  ",
                          kommentar="   ").status_code == 201
        vers = client.get(f"/books/{book}/verifikationer-full").json()
        assert [v["ext_ref"] for v in vers] == [None, None]
        assert [v["kommentar"] for v in vers] == [None, None]

    def test_an_inkops_kvittonummer_reaches_its_verifikation(self, client, book):
        cid = client.post(f"/books/{book}/categories",
                          json={"name": "Förbrukning", "kind": "expense",
                                "bas_konto": 5460}).json()["id"]
        client.post(f"/books/{book}/expenses", json={
            "category_id": cid, "trans_date": "2026-02-01", "paid_date": "2026-02-01",
            "ext_ref": "KVITTO-991",
            "lines": [{"rate_code": "25", "amount_ore": 1250}]})
        ver = client.get(f"/books/{book}/verifikationer-full").json()[0]
        assert ver["ext_ref"] == "KVITTO-991"


class TestAssetPurchase:
    """Inventarieinköp: the firma buys a tool. Deductible moms; konto depends on price."""

    def test_expensive_tool_is_capitalised_and_is_not_a_cost_of_the_year(self, client, book):
        # 2026 prisbasbelopp 59 200 -> threshold 29 600 kr ex moms.
        prev = client.get(f"/books/{book}/asset-purchase/preview",
                          params={"amount_ore": 5000000}).json()
        assert prev["threshold_ore"] == 2960000
        assert prev["suggested"] == "aktivera" and prev["bas_konto"] == 1220
        assert prev["ex_moms_ore"] == 4000000 and prev["moms_ore"] == 1000000

        res = client.post(f"/books/{book}/asset-purchase", json={
            "description": "Lödstation JBC, serienr X1", "amount_ore": 5000000,
            "trans_date": "2026-04-01", "paid_date": "2026-04-01", "ext_ref": "F-123"})
        assert res.status_code == 201 and res.json()["treatment"] == "aktivera"

        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[1220] == 4000000          # asset, not a cost
        assert hb[2640] == 1000000          # moms IS deductible here
        assert hb[1930] == -5000000
        assert sum(hb.values()) == 0

        # The result report must NOT treat it as a cost of the year...
        r = client.get(f"/books/{book}/reports/result",
                       params={"start": "2026-01-01", "end": "2026-12-31"}).json()
        assert r["expense_ore"] == 0
        # ...and the årsbokslut must agree with it.
        ab = client.get(f"/books/{book}/reports/arsbokslut",
                        params={"start": "2026-01-01", "end": "2026-12-31"}).json()
        assert ab["arets_resultat_ore"] == 0 and ab["balanserar"] is True
        # The moms still reaches the momsdeklaration.
        assert client.get(f"/books/{book}/reports/momsdeklaration",
                          params={"start": "2026-01-01", "end": "2026-06-30"}
                          ).json()["boxes"]["48"] == 1000000

    def test_cheap_tool_is_expensed_directly(self, client, book):
        res = client.post(f"/books/{book}/asset-purchase", json={
            "description": "Skruvdragare", "amount_ore": 250000,
            "trans_date": "2026-02-01", "paid_date": "2026-02-01"}).json()
        assert res["treatment"] == "direktavdrag" and res["bas_konto"] == 5410
        r = client.get(f"/books/{book}/reports/result",
                       params={"start": "2026-01-01", "end": "2026-12-31"}).json()
        assert r["expense_ore"] == 200000

    def test_three_year_rule_allows_direct_deduction_above_the_threshold(self, client, book):
        prev = client.get(f"/books/{book}/asset-purchase/preview",
                          params={"amount_ore": 5000000, "useful_life_years": 3}).json()
        assert prev["suggested"] == "direktavdrag" and prev["bas_konto"] == 5410
        assert any("18 kap. 4" in n for n in prev["notes"])
        # four years -> back to capitalising
        assert client.get(f"/books/{book}/asset-purchase/preview",
                          params={"amount_ore": 5000000, "useful_life_years": 4}
                          ).json()["suggested"] == "aktivera"

    def test_mode_and_konto_override(self, client, book):
        res = client.post(f"/books/{book}/asset-purchase", json={
            "description": "Dator", "amount_ore": 5000000, "trans_date": "2026-04-01",
            "mode": "aktivera", "konto": 1250, "paid_date": "2026-04-01"}).json()
        assert res["bas_konto"] == 1250
        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[1250] == 4000000

    def test_unpaid_becomes_a_leverantorsfaktura_booked_on_payment(self, client, book):
        res = client.post(f"/books/{book}/asset-purchase", json={
            "description": "Mätinstrument", "amount_ore": 5000000,
            "trans_date": "2026-04-01", "ext_ref": "LF-9"}).json()
        tid = res["transaktion_id"]
        assert "verifikation_id" not in res
        rows = client.get(f"/books/{book}/transaktioner").json()
        row = [t for t in rows if t["id"] == tid][0]
        assert row["status"] == "pending" and row["ext_ref"] == "LF-9"
        assert row["konto_label"].startswith("1220 ")     # labelled by konto, not category
        assert client.post(f"/books/{book}/transaktioner/{tid}/pay",
                           json={"payment_date": "2026-05-01"}).status_code == 200
        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[1220] == 4000000 and hb[1930] == -5000000

    def test_private_account_and_receipt_and_soft_delete_all_work(self, client, book):
        res = client.post(f"/books/{book}/asset-purchase", json={
            "description": "Verktyg", "amount_ore": 5000000, "trans_date": "2026-04-01",
            "paid_date": "2026-04-01", "paid_account": "privat"}).json()
        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[2018] == -5000000 and 1930 not in hb

        pending = client.post(f"/books/{book}/asset-purchase", json={
            "description": "Annat", "amount_ore": 100000, "trans_date": "2026-04-02"}).json()
        tid = pending["transaktion_id"]
        assert client.post(f"/books/{book}/transaktioner/{tid}/delete").status_code == 200
        assert all(t["id"] != tid for t in client.get(f"/books/{book}/transaktioner").json())

    def test_guards(self, client, book):
        base = {"description": "X", "amount_ore": 100000, "trans_date": "2026-04-01"}
        assert client.post(f"/books/{book}/asset-purchase",
                           json={**base, "description": " "}).status_code == 400
        assert client.post(f"/books/{book}/asset-purchase",
                           json={**base, "amount_ore": 0}).status_code == 400
        assert client.post(f"/books/{book}/asset-purchase",
                           json={**base, "mode": "nonsens"}).status_code == 400
        # An asset purchase must not be edited through the ordinary inköp editor, which
        # would rebuild its moms lines from a category it does not have.
        tid = client.post(f"/books/{book}/asset-purchase", json=base).json()["transaktion_id"]
        assert client.get(f"/books/{book}/transaktioner/{tid}/edit-payload").status_code == 409
        booked = client.post(f"/books/{book}/asset-purchase",
                             json={**base, "paid_date": "2026-04-01"}).json()["transaktion_id"]
        assert client.post(f"/books/{book}/transaktioner/{booked}/rebook",
                           json={"corrections": {}}).status_code == 409


class TestDepreciation:
    """Anläggningsregister + årets avskrivning (7832 debet / 1229 kredit)."""

    def _buy(self, client, book, amount_ore=5000000, date="2026-04-01", life=None):
        body = {"description": "Lödstation JBC", "amount_ore": amount_ore,
                "trans_date": date, "paid_date": date, "mode": "aktivera"}
        if life:
            body["useful_life_years"] = life
        return client.post(f"/books/{book}/asset-purchase", json=body).json()

    def test_capitalising_registers_the_asset(self, client, book):
        res = self._buy(client, book)
        assert res["fixed_asset_id"]
        a = client.get(f"/books/{book}/fixed-assets").json()[0]
        assert a["acquisition_ore"] == 4000000        # ex moms
        assert a["asset_konto"] == 1220 and a["accumulated_konto"] == 1229
        assert a["expense_konto"] == 7832
        assert a["useful_life_years"] == 5            # config default
        assert a["book_value_ore"] == 4000000 and a["accumulated_ore"] == 0

    def test_a_direct_deduction_is_not_registered(self, client, book):
        res = client.post(f"/books/{book}/asset-purchase", json={
            "description": "Skruvdragare", "amount_ore": 250000,
            "trans_date": "2026-02-01", "paid_date": "2026-02-01"}).json()
        assert "fixed_asset_id" not in res
        assert client.get(f"/books/{book}/fixed-assets").json() == []

    def test_proposal_and_booking(self, client, book):
        self._buy(client, book)                        # 40 000 kr ex moms, 5 år
        prop = client.get(f"/books/{book}/depreciations/proposal",
                          params={"fiscal_year_end": "2026-12-31"}).json()
        assert prop["total_ore"] == 800000             # 40 000 / 5 = 8 000 kr
        assert prop["items"][0]["skipped"] is False

        res = client.post(f"/books/{book}/depreciations",
                          json={"fiscal_year_end": "2026-12-31"})
        assert res.status_code == 201 and res.json()["total_ore"] == 800000

        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[7832] == 800000 and hb[1229] == -800000
        assert hb[1220] == 4000000                     # the asset itself is untouched
        assert sum(hb.values()) == 0

        # Now it IS a cost of the year, unlike the purchase itself. The årsbokslut (and
        # therefore the Skatt tab) reads the raw postings, so it picks the depreciation up.
        ab = client.get(f"/books/{book}/reports/arsbokslut",
                        params={"start": "2026-01-01", "end": "2026-12-31"}).json()
        assert ab["arets_resultat_ore"] == -800000 and ab["balanserar"] is True

        a = client.get(f"/books/{book}/fixed-assets").json()[0]
        assert a["accumulated_ore"] == 800000 and a["book_value_ore"] == 3200000

    def test_a_year_cannot_be_booked_twice(self, client, book):
        self._buy(client, book)
        client.post(f"/books/{book}/depreciations", json={"fiscal_year_end": "2026-12-31"})
        prop = client.get(f"/books/{book}/depreciations/proposal",
                          params={"fiscal_year_end": "2026-12-31"}).json()
        assert prop["total_ore"] == 0
        assert prop["items"][0]["already_booked"] is True
        assert client.post(f"/books/{book}/depreciations",
                           json={"fiscal_year_end": "2026-12-31"}).status_code == 409

    def test_last_year_takes_the_remainder_and_then_stops(self, client, book):
        # 10 000 kr ex moms over 3 years -> 3333,33... so the years must still sum exactly.
        self._buy(client, book, amount_ore=1250000, life=3)
        booked = []
        for year in ("2026-12-31", "2027-12-31", "2028-12-31"):
            booked.append(client.post(f"/books/{book}/depreciations",
                                      json={"fiscal_year_end": year}).json()["total_ore"])
        assert sum(booked) == 1000000                  # exactly the anskaffningsvärde
        a = client.get(f"/books/{book}/fixed-assets").json()[0]
        assert a["book_value_ore"] == 0 and a["fully_depreciated"] is True
        hb = {x["bas_konto"]: x["saldo_ore"] for x in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[1220] + hb[1229] == 0                # net book value in the ledger is 0
        # a fourth year has nothing left to write off
        assert client.post(f"/books/{book}/depreciations",
                           json={"fiscal_year_end": "2029-12-31"}).status_code == 409

    def test_override_and_skip(self, client, book):
        a = self._buy(client, book)["fixed_asset_id"]
        res = client.post(f"/books/{book}/depreciations", json={
            "fiscal_year_end": "2026-12-31", "overrides": {str(a): 500000}}).json()
        assert res["total_ore"] == 500000
        # an override above the remaining book value is refused
        b = self._buy(client, book, date="2027-01-05")["fixed_asset_id"]
        assert client.post(f"/books/{book}/depreciations", json={
            "fiscal_year_end": "2027-12-31",
            "overrides": {str(b): 99000000}}).status_code == 400

    def test_asset_bought_after_year_end_and_disposed_are_skipped(self, client, book):
        late = self._buy(client, book, date="2027-03-01")["fixed_asset_id"]
        prop = client.get(f"/books/{book}/depreciations/proposal",
                          params={"fiscal_year_end": "2026-12-31"}).json()
        item = [i for i in prop["items"] if i["id"] == late][0]
        assert item["skipped"] and "efter" in item["reason"]

        client.patch(f"/books/{book}/fixed-assets/{late}", json={"disposed_date": "2027-06-01"})
        prop = client.get(f"/books/{book}/depreciations/proposal",
                          params={"fiscal_year_end": "2028-12-31"}).json()
        assert [i for i in prop["items"] if i["id"] == late][0]["skipped"]

    def test_manual_register_entry_and_delete_guard(self, client, book):
        aid = client.post(f"/books/{book}/fixed-assets", json={
            "description": "Gammal maskin", "acquisition_ore": 3000000,
            "acquired_date": "2024-01-01", "useful_life_years": 10}).json()["id"]
        assert client.get(f"/books/{book}/fixed-assets").json()[0]["annual_ore"] == 300000
        assert client.delete(f"/books/{book}/fixed-assets/{aid}").status_code == 200
        aid2 = client.post(f"/books/{book}/fixed-assets", json={
            "description": "M2", "acquisition_ore": 3000000,
            "acquired_date": "2024-01-01"}).json()["id"]
        client.post(f"/books/{book}/depreciations", json={"fiscal_year_end": "2026-12-31"})
        assert client.delete(f"/books/{book}/fixed-assets/{aid2}").status_code == 409

    def test_depreciation_respects_a_locked_period(self, client, book):
        self._buy(client, book)
        client.post(f"/books/{book}/period-locks",
                    json={"period_start": "2026-01-01", "period_end": "2026-12-31"})
        assert client.post(f"/books/{book}/depreciations",
                           json={"fiscal_year_end": "2026-12-31"}).status_code == 409


class TestPerLineExpenseKonto:
    """One receipt, several kinds of thing: a default konto plus per-line overrides."""

    def _cats(self, client, book):
        mk = lambda n, k: client.post(f"/books/{book}/categories",
                                      json={"name": n, "kind": "expense",
                                            "bas_konto": k}).json()["id"]
        return (mk("Förbrukningsinventarier", 5410), mk("Förbrukningsmaterial", 5460),
                mk("Programvaror", 5420))

    def test_one_purchase_splits_across_three_konton(self, client, book):
        verktyg, material, program = self._cats(client, book)
        res = client.post(f"/books/{book}/expenses", json={
            "category_id": verktyg,                 # the default for the receipt
            "trans_date": "2026-05-01", "paid_date": "2026-05-01", "ext_ref": "KV-1",
            "items": [
                # no override -> falls back to the default konto
                {"quantity_centi": 100, "unit_cost_ore": 100000, "rate_code": "25"},
                {"quantity_centi": 100, "unit_cost_ore": 40000, "rate_code": "25",
                 "expense_category_id": material},
                {"quantity_centi": 100, "unit_cost_ore": 60000, "rate_code": "25",
                 "expense_category_id": program},
            ]})
        assert res.status_code == 201

        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[5410] == 100000 and hb[5460] == 40000 and hb[5420] == 60000
        assert hb[2640] == 50000                    # 25 % of 2 000 kr
        assert hb[1930] == -250000
        assert sum(hb.values()) == 0

        # the result report splits the same way
        by = {r["bas_konto"]: r["amount_ore"] for r in client.get(
            f"/books/{book}/reports/result",
            params={"start": "2026-01-01", "end": "2026-12-31"}).json()["by_category"]}
        assert by == {5410: 100000, 5460: 40000, 5420: 60000}

    def test_override_must_be_an_expense_konto(self, client, book):
        verktyg, _, _ = self._cats(client, book)
        income = client.post(f"/books/{book}/categories",
                             json={"name": "Försäljning", "kind": "income",
                                   "bas_konto": 3041}).json()["id"]
        assert client.post(f"/books/{book}/expenses", json={
            "category_id": verktyg, "trans_date": "2026-05-01",
            "items": [{"quantity_centi": 100, "unit_cost_ore": 10000, "rate_code": "25",
                       "expense_category_id": income}]}).status_code in (400, 409)

    def test_per_line_konto_survives_an_edit_round_trip(self, client, book):
        verktyg, material, _ = self._cats(client, book)
        tid = client.post(f"/books/{book}/expenses", json={
            "category_id": verktyg, "trans_date": "2026-05-01",
            "items": [{"quantity_centi": 100, "unit_cost_ore": 100000, "rate_code": "25"},
                      {"quantity_centi": 100, "unit_cost_ore": 40000, "rate_code": "25",
                       "expense_category_id": material}]}).json()["transaktion_id"]
        payload = client.get(f"/books/{book}/transaktioner/{tid}/edit-payload").json()
        overrides = {i.get("expense_category_id") for i in payload["items"]}
        assert overrides == {None, material}

    def test_a_single_konto_purchase_is_unchanged(self, client, book):
        verktyg, _, _ = self._cats(client, book)
        client.post(f"/books/{book}/expenses", json={
            "category_id": verktyg, "trans_date": "2026-05-01", "paid_date": "2026-05-01",
            "items": [{"quantity_centi": 100, "unit_cost_ore": 100000, "rate_code": "25"}]})
        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[5410] == 100000 and 5460 not in hb

    def test_bokfor_form_lines_can_override_the_konto_too(self, client, book):
        """The same per-line konto works for a plain income/expense entry."""
        verktyg, material, _ = self._cats(client, book)
        client.post(f"/books/{book}/expenses", json={
            "category_id": verktyg, "trans_date": "2026-06-01", "paid_date": "2026-06-01",
            "lines": [{"rate_code": "25", "amount_ore": 125000},
                      {"rate_code": "25", "amount_ore": 50000,
                       "category_id": material}]})
        hb = {a["bas_konto"]: a["saldo_ore"] for a in client.get(f"/books/{book}/huvudbok").json()}
        assert hb[5410] == 100000 and hb[5460] == 40000
        assert hb[2640] == 35000 and sum(hb.values()) == 0

    def test_income_line_override_must_be_an_income_konto(self, client, book):
        verktyg, _, _ = self._cats(client, book)
        sales = client.post(f"/books/{book}/categories",
                            json={"name": "Tjänster", "kind": "income",
                                  "bas_konto": 3041}).json()["id"]
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "K AB"}).json()["kundnummer"]
        assert client.post(f"/books/{book}/incomes", json={
            "customer_id": kid, "category_id": sales, "trans_date": "2026-06-01",
            "lines": [{"rate_code": "25", "amount_ore": 12500,
                       "category_id": verktyg}]}).status_code in (400, 409)


class TestResultReportOnPostings:
    """The result report reads the raw postings, so it agrees with the årsbokslut."""

    def _konto(self, client, book, name, kind, bas):
        return client.post(f"/books/{book}/categories",
                           json={"name": name, "kind": kind, "bas_konto": bas}).json()["id"]

    def _result(self, client, book):
        return client.get(f"/books/{book}/reports/result",
                          params={"start": "2026-01-01", "end": "2026-12-31"}).json()

    def _arsbokslut(self, client, book):
        return client.get(f"/books/{book}/reports/arsbokslut",
                          params={"start": "2026-01-01", "end": "2026-12-31"}).json()

    def test_a_manual_verifikation_now_reaches_the_report(self, client, book):
        # Nothing but a hand-written entry: it used to be invisible here.
        client.post(f"/books/{book}/verifikationer/manual", json={
            "ver_date": "2026-03-01", "text": "Bankavgift",
            "postings": [{"bas_konto": 6570, "debit_ore": 25000, "credit_ore": 0},
                         {"bas_konto": 1930, "debit_ore": 0, "credit_ore": 25000}]})
        r = self._result(client, book)
        assert r["expense_ore"] == 25000 and r["result_ore"] == -25000
        assert {x["bas_konto"] for x in r["by_category"]} == {6570}
        # and it matches the årsbokslut, which has always read the postings
        assert self._arsbokslut(client, book)["arets_resultat_ore"] == r["result_ore"]

    def test_depreciation_reaches_the_report(self, client, book):
        client.post(f"/books/{book}/asset-purchase", json={
            "description": "Maskin", "amount_ore": 5000000, "trans_date": "2026-04-01",
            "paid_date": "2026-04-01", "mode": "aktivera"})
        client.post(f"/books/{book}/depreciations", json={"fiscal_year_end": "2026-12-31"})
        r = self._result(client, book)
        # the purchase itself is a balance-sheet item; only the write-off is a cost
        assert r["expense_ore"] == 800000
        assert {x["bas_konto"] for x in r["by_category"]} == {7832}
        assert self._arsbokslut(client, book)["arets_resultat_ore"] == r["result_ore"]

    def test_ordinary_income_and_expense_still_read_the_same(self, client, book):
        sales = self._konto(client, book, "Tjänster", "income", 3041)
        cost = self._konto(client, book, "Förbrukning", "expense", 5460)
        kid = client.post(f"/books/{book}/customers",
                          json={"type": "business", "company_name": "K AB"}).json()["kundnummer"]
        client.post(f"/books/{book}/incomes", json={
            "customer_id": kid, "category_id": sales, "trans_date": "2026-02-01",
            "paid_date": "2026-02-01",
            "lines": [{"rate_code": "25", "amount_ore": 125000}]})
        client.post(f"/books/{book}/expenses", json={
            "category_id": cost, "trans_date": "2026-02-10", "paid_date": "2026-02-10",
            "lines": [{"rate_code": "25", "amount_ore": 62500}]})
        r = self._result(client, book)
        assert r["income_ore"] == 100000 and r["expense_ore"] == 50000
        assert r["result_ore"] == 50000
        by = {x["bas_konto"]: x for x in r["by_category"]}
        assert by[3041]["kind"] == "income" and by[3041]["name"] == "Tjänster"
        assert by[5460]["kind"] == "expense" and by[5460]["amount_ore"] == 50000
        # moms konton are balance-sheet and must never show up as income/cost
        assert 2610 not in by and 2640 not in by
        assert self._arsbokslut(client, book)["arets_resultat_ore"] == r["result_ore"]

    def test_a_rattelse_nets_the_konto_out(self, client, book):
        cost = self._konto(client, book, "Förbrukning", "expense", 5460)
        res = client.post(f"/books/{book}/expenses", json={
            "category_id": cost, "trans_date": "2026-02-10", "paid_date": "2026-02-10",
            "lines": [{"rate_code": "25", "amount_ore": 62500}]}).json()
        client.post(f"/books/{book}/verifikationer/{res['verifikation_id']}/reverse",
                    json={"reason": "fel"})
        r = self._result(client, book)
        assert r["expense_ore"] == 0
        assert all(x["bas_konto"] != 5460 for x in r["by_category"])   # nets to zero

    def test_financial_income_and_cost_are_split_by_konto(self, client, book):
        client.post(f"/books/{book}/verifikationer/manual", json={
            "ver_date": "2026-03-01", "text": "Ränta",
            "postings": [{"bas_konto": 1930, "debit_ore": 5000, "credit_ore": 0},
                         {"bas_konto": 8310, "debit_ore": 0, "credit_ore": 5000}]})
        client.post(f"/books/{book}/verifikationer/manual", json={
            "ver_date": "2026-03-02", "text": "Räntekostnad",
            "postings": [{"bas_konto": 8410, "debit_ore": 3000, "credit_ore": 0},
                         {"bas_konto": 1930, "debit_ore": 0, "credit_ore": 3000}]})
        r = self._result(client, book)
        assert r["income_ore"] == 5000 and r["expense_ore"] == 3000
        by = {x["bas_konto"]: x["kind"] for x in r["by_category"]}
        assert by[8310] == "income" and by[8410] == "expense"


class TestRebookNeverErasesTheEntry:
    """Regressions for a rebook that reversed the entry and then failed to re-book it,
    leaving the amount erased from the books (both halves netted to zero)."""

    def _setup(self, client, book, *, paid_account="bank"):
        wrong = client.post(f"/books/{book}/categories",
                            json={"name": "Fel (3003)", "kind": "expense",
                                  "bas_konto": 3003}).json()["id"]
        right = client.post(f"/books/{book}/categories",
                            json={"name": "Varor", "kind": "expense",
                                  "bas_konto": 4010}).json()["id"]
        tid = client.post(f"/books/{book}/expenses", json={
            "category_id": wrong, "trans_date": "2026-02-25",
            "lines": [{"rate_code": "25", "amount_ore": 402040}]}).json()["transaktion_id"]
        client.post(f"/books/{book}/transaktioner/{tid}/pay",
                    json={"payment_date": "2026-02-25", "paid_account": paid_account})
        mlid = client.get(f"/books/{book}/transaktioner/{tid}/lines").json()[0]["id"]
        return tid, mlid, right

    def _hb(self, client, book):
        return {a["bas_konto"]: a["saldo_ore"]
                for a in client.get(f"/books/{book}/huvudbok").json()}

    def test_rebook_of_a_purchase_paid_privately_keeps_the_settlement(self, client, book):
        # 2018 Egna insättningar was dropped by the old whitelist, so the re-booking
        # could not balance and the whole purchase vanished.
        tid, mlid, right = self._setup(client, book, paid_account="privat")
        r = client.post(f"/books/{book}/transaktioner/{tid}/rebook",
                        json={"corrections": {str(mlid): {"category_id": right}}})
        assert r.status_code == 201
        hb = self._hb(client, book)
        assert hb.get(3003, 0) == 0            # the wrong konto is cleared
        assert hb[4010] == 321632              # ...and the cost really moved
        assert hb[2640] == 80408               # moms still deducted
        assert hb[2018] == -402040             # settlement preserved, NOT erased
        assert sum(hb.values()) == 0

    def test_rebook_into_a_locked_period_changes_nothing(self, client, book):
        # The reversal is dated today and passes; the re-booking used to be dated back to
        # the original and hit the lock AFTER the reversal had committed.
        tid, mlid, right = self._setup(client, book)
        before = self._hb(client, book)
        client.post(f"/books/{book}/period-locks",
                    json={"period_start": "2026-01-01", "period_end": "2026-12-31"})
        r = client.post(f"/books/{book}/transaktioner/{tid}/rebook",
                        json={"corrections": {str(mlid): {"category_id": right}}})
        assert r.status_code == 409
        # nothing reversed, nothing lost — the books are exactly as before
        assert self._hb(client, book) == before
        assert before[3003] == 321632

    def test_both_halves_land_in_the_same_period(self, client, book):
        tid, mlid, right = self._setup(client, book)
        feb_before = client.get(f"/books/{book}/reports/result",
                                params={"start": "2026-02-01", "end": "2026-02-28"}).json()
        client.post(f"/books/{book}/transaktioner/{tid}/rebook",
                    json={"corrections": {str(mlid): {"category_id": right}}})
        vers = client.get(f"/books/{book}/verifikationer-full").json()
        rattelse = [v for v in vers if v["text"].startswith("Rättelse")][0]
        ombok = [v for v in vers if v["text"].startswith("Ombokföring")][0]
        assert rattelse["ver_date"] == ombok["ver_date"], "paret måste netta i samma period"

        # The original period is untouched — the original verifikation is immutable and
        # the correction lands wholly in the period it was made in. Previously the
        # re-booking was dated back here while the reversal was not, so this period ended
        # up carrying the cost twice.
        feb_after = client.get(f"/books/{book}/reports/result",
                               params={"start": "2026-02-01", "end": "2026-02-28"}).json()
        assert feb_after["by_category"] == feb_before["by_category"]

        # In the correction's own period the amount moves off 3003 and onto 4010. 3003 is
        # an INCOME konto — booking a cost there is exactly the mistake being corrected —
        # so undoing it reads as +income while the cost appears on 4010. The two cancel:
        # a pure reclassification must not change any period's result.
        korr = client.get(f"/books/{book}/reports/result",
                          params={"start": ombok["ver_date"], "end": ombok["ver_date"]}).json()
        by = {x["bas_konto"]: x["amount_ore"] for x in korr["by_category"]}
        assert by[3003] == 321632 and by[4010] == 321632
        assert korr["result_ore"] == 0

    def test_an_imbalanced_rebook_is_refused_before_reversing(self, client, book):
        tid, mlid, right = self._setup(client, book)
        before = self._hb(client, book)
        # a bogus konto id cannot be corrected to -> refused, nothing touched
        r = client.post(f"/books/{book}/transaktioner/{tid}/rebook",
                        json={"corrections": {str(mlid): {"category_id": 999999}}})
        assert r.status_code in (400, 404, 409)
        assert self._hb(client, book) == before


class TestArsbokslutPriorYearResult:
    """A result-konto posting dated before the fiscal year sits in the balance sheet
    through its counter-posting; its result side belongs in eget kapital."""

    def test_earlier_years_result_is_carried_into_eget_kapital(self, client, book):
        client.post(f"/books/{book}/verifikationer/manual", json={
            "ver_date": "2025-12-20", "text": "Bankavgift 2025",
            "postings": [{"bas_konto": 6570, "debit_ore": 1600, "credit_ore": 0},
                         {"bas_konto": 1930, "debit_ore": 0, "credit_ore": 1600}]})
        ab = client.get(f"/books/{book}/reports/arsbokslut",
                        params={"start": "2026-01-01", "end": "2026-12-31"}).json()
        assert ab["tidigare_resultat_ore"] == -1600      # last year's loss
        assert ab["diff_ore"] == 0 and ab["balanserar"] is True
        assert ab["arets_resultat_ore"] == 0            # nothing happened in 2026
        b10 = ab["balans"]["B10"]
        assert any(a["name"].startswith("Balanserat resultat") for a in b10["accounts"])

    def test_the_first_year_is_unaffected(self, client, book):
        client.post(f"/books/{book}/verifikationer/manual", json={
            "ver_date": "2026-03-01", "text": "Bankavgift",
            "postings": [{"bas_konto": 6570, "debit_ore": 1600, "credit_ore": 0},
                         {"bas_konto": 1930, "debit_ore": 0, "credit_ore": 1600}]})
        ab = client.get(f"/books/{book}/reports/arsbokslut",
                        params={"start": "2026-01-01", "end": "2026-12-31"}).json()
        assert ab["tidigare_resultat_ore"] == 0
        assert ab["balanserar"] is True and ab["arets_resultat_ore"] == -1600
