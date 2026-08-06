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

    def test_reverse_creates_rattelse(self, client, book):
        res = self._setup_income(client, book).json()
        vid = res["verifikation_id"]
        rev = client.post(f"/books/{book}/verifikationer/{vid}/reverse",
                          json={"reason": "fel"})
        assert rev.status_code == 201
        assert rev.json()["ver_number"] == 2

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
        # inc 1 249 kr -> 30 min support
        client.post(f"/books/{book}/invoices", json={
            "customer_id": kid, "category_id": cat, "invoice_date": "2026-03-15",
            "due_date": "2026-04-15",
            "lines": [{"description": "IT", "quantity_centi": 100,
                       "unit_price_ore": round(124900 / 1.25), "rate_code": "25"}]})
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
