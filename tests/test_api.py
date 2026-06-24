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
            app.state.autolock_seconds = 1
            app.state.last_activity[bid] = time.monotonic() - 10
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
