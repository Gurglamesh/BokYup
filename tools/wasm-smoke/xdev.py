#!/usr/bin/env python3
"""
Cross-device .buyn round-trip — the NATIVE (PC) half.

Proves a book exported on a PC imports on the phone and vice-versa, by exercising
the real backend on native CPython and pairing with xdev_wasm.mjs which runs the
same backend under Pyodide (WASM). See README.md for the full run.

  export <workdir>         create+populate a book, export <workdir>/pc.buyn
  verify-import <workdir>  import <workdir>/phone.buyn (made under WASM), assert
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.api.phone import PhoneApp  # noqa: E402

PC_PASS = "pc-secret-pass"
PHONE_PASS = "phone-secret-pass"


def _jcall(app, *a):
    return json.loads(app.call(*a))


def export(workdir: Path) -> None:
    app = PhoneApp(workdir / "pc_data", autolock_seconds=900)
    bid = _jcall(app, "POST", "/books", json.dumps(
        {"display_name": "PC AB", "db_path": str(workdir / "pc_data/pc.db"),
         "passphrase": PC_PASS}))["result"]["id"]
    cat = _jcall(app, "POST", f"/books/{bid}/categories",
                 json.dumps({"name": "Städ", "kind": "income", "bas_konto": 3001}))["result"]["id"]
    kid = _jcall(app, "POST", f"/books/{bid}/customers", json.dumps(
        {"type": "private", "first_name": "Anna", "last_name": "Svensson",
         "personnummer": "811218-9876"}))["result"]["kundnummer"]
    inc = _jcall(app, "POST", f"/books/{bid}/incomes", json.dumps(
        {"customer_id": kid, "category_id": cat,
         "lines": [{"rate_code": "25", "amount_ore": 12500, "inclusive": True}],
         "trans_date": "2026-03-10", "paid_date": "2026-03-10"}))
    tid = inc["result"]["transaktion_id"]

    photo = bytes((i * 7) % 256 for i in range(4096))
    rid = _jcall(app, "POST", f"/books/{bid}/transaktioner/{tid}/receipts", json.dumps(
        {"image_base64": base64.b64encode(photo).decode(), "mime": "image/png",
         "original_format": "digital"}))["result"]["id"]

    _jcall(app, "POST", f"/books/{bid}/export",
           json.dumps({"out_path": str(workdir / "pc.buyn")}))
    (workdir / "pc_expected.json").write_text(json.dumps({
        "passphrase": PC_PASS, "customer_personnummer": "811218-9876",
        "customer_name": "Anna Svensson", "ver_number": inc["result"]["ver_number"],
        "receipt_id": rid, "receipt_b64": base64.b64encode(photo).decode(),
    }))
    print("PC exported", workdir / "pc.buyn")


def verify_import(workdir: Path) -> None:
    app = PhoneApp(workdir / "pc_reimport", autolock_seconds=900)
    bid = _jcall(app, "POST", "/books/import", json.dumps(
        {"bundle_path": str(workdir / "phone.buyn"),
         "dest_db_path": str(workdir / "pc_reimport/from_phone.db"),
         "display_name": "From phone"}))["result"]["id"]
    assert _jcall(app, "POST", f"/books/{bid}/unlock",
                  json.dumps({"passphrase": PHONE_PASS}))["result"]["unlocked"]
    cust = _jcall(app, "GET", f"/books/{bid}/customers/1")["result"]
    rep = _jcall(app, "GET", f"/books/{bid}/reports/momsdeklaration", None,
                 json.dumps({"start": "2026-01-01", "end": "2026-12-31"}))["result"]
    assert f"{cust['first_name']} {cust['last_name']}" == "Björn Berg"
    assert cust["personnummer"] == "19811218-9876"
    assert rep["boxes"]["11"] == 600  # 5600 öre incl 12% -> 600 moms
    print("phone -> PC import verified (native)")


if __name__ == "__main__":
    cmd, wd = sys.argv[1], Path(sys.argv[2])
    wd.mkdir(parents=True, exist_ok=True)
    {"export": export, "verify-import": verify_import}[cmd](wd)
