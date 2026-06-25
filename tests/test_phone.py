"""
Tests for the phone JSON boundary (backend/api/phone.py).

This is the exact entry point the phone's JavaScript shim calls under Pyodide.
Driving it natively here keeps the contract (status codes, ok flag, raw payloads)
verified in the normal test run; the WASM harness proves the same module loads and
runs under Pyodide.
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from backend.api.phone import PhoneApp


@pytest.fixture()
def app(tmp_path: Path) -> PhoneApp:
    return PhoneApp(tmp_path / "reg", autolock_seconds=900)


def _call(app, *args):
    return json.loads(app.call(*args))


def test_full_flow_and_raw_and_errors(app, tmp_path):
    r = _call(app, "POST", "/books",
              json.dumps({"display_name": "X", "db_path": str(tmp_path / "b.db"),
                          "passphrase": "pw"}))
    assert r["ok"] and r["status"] == 201
    bid = r["result"]["id"]

    cat = _call(app, "POST", f"/books/{bid}/categories",
                json.dumps({"name": "F", "kind": "income", "bas_konto": 3001}))["result"]["id"]
    kid = _call(app, "POST", f"/books/{bid}/customers",
                json.dumps({"type": "business", "company_name": "ACME"}))["result"]["kundnummer"]
    inc = _call(app, "POST", f"/books/{bid}/incomes", json.dumps({
        "customer_id": kid, "category_id": cat,
        "lines": [{"rate_code": "25", "amount_ore": 1250, "inclusive": True}],
        "trans_date": "2026-02-10", "paid_date": "2026-02-10"}))
    assert inc["ok"] and inc["result"]["ver_number"] == 1

    # SIE comes back as raw text (frontend builds a Blob from it).
    sie = _call(app, "GET", f"/books/{bid}/reports/sie", None,
                json.dumps({"company_name": "Min Firma"}))
    assert sie["result"]["raw"] and "#FNAMN" in sie["result"]["text"]

    # Locked book -> 423; wrong passphrase -> 401 (same codes as HTTP).
    app.call("POST", f"/books/{bid}/lock")
    locked = _call(app, "GET", f"/books/{bid}/categories")
    assert locked["ok"] is False and locked["status"] == 423
    bad = _call(app, "POST", f"/books/{bid}/unlock", json.dumps({"passphrase": "nope"}))
    assert bad["status"] == 401
