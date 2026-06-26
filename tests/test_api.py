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

    def test_reverse_creates_rattelse(self, client, book):
        res = self._setup_income(client, book).json()
        vid = res["verifikation_id"]
        rev = client.post(f"/books/{book}/verifikationer/{vid}/reverse",
                          json={"reason": "fel"})
        assert rev.status_code == 201
        assert rev.json()["ver_number"] == 2

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
        assert len(lst) == 1 and lst[0]["status"] == "pending"
        got = client.get(f"/books/{book}/invoices/{inv['invoice_id']}").json()
        assert got["seller"]["name"] == "Min Firma AB"
        assert got["buyer"]["first_name"] == "Anna"
        assert got["payment_methods"][0]["label"] == "Swish"

    def test_invoice_with_rut_household_split(self, client, book):
        cat, kid = self._setup(client, book)
        inv = client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-01",
            "due_date": "2026-03-31",
            "lines": [{"description": "Städ", "quantity_centi": 100, "unit_price_ore": 1000000,
                       "rate_code": "25", "rut_eligible": True}],
            "recipients": [{"first_name": "Anna", "last_name": "Svensson",
                            "personnummer": "811218-9876", "rut_amount_ore": 150000},
                           {"first_name": "Björn", "last_name": "Svensson",
                            "personnummer": "19811218-9876", "rut_amount_ore": 100000}]}).json()
        assert inv["rut_total_ore"] == 250000
        got = client.get(f"/books/{book}/invoices/{inv['invoice_id']}").json()
        assert len(got["recipients"]) == 2

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
