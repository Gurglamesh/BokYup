"""
csv_export.py — huvudbok / grundbok as CSV, for Excel and for a revisor.

Two views of the same postings:

  * **huvudbok** — one row per kontering, grouped by BAS-konto, with a running saldo
    per konto. This is the ledger you read when you want to know what a konto consists of.
  * **grundbok** — one row per kontering in verifikation order. The journal.

Written for **Excel in a Swedish locale**, which is where these files actually get
opened: semicolon-separated (a comma-separated file with decimal commas cannot be parsed
unambiguously), decimal comma, and a UTF-8 BOM so å/ä/ö survive a double-click. Amounts
are plain numbers without a thousands separator so Excel reads them as numbers, and debit
and credit sit in separate columns the way a bookkeeper expects — never one signed column.

SIE (`sie.py`) remains the format to hand a revisor for import; this is for reading,
filtering and pivoting.
"""

from __future__ import annotations

import csv
import io
from typing import Optional

BOM = "﻿"


def _kr(ore: int) -> str:
    """Integer ören -> '1234,56'. No thousands separator: Excel must see a number."""
    return f"{ore / 100:.2f}".replace(".", ",")


def _writer():
    buf = io.StringIO()
    # QUOTE_MINIMAL + \r\n is what Excel writes itself, so a round trip is lossless.
    return buf, csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL,
                           lineterminator="\r\n")


def huvudbok_csv(accounts: list[dict]) -> str:
    """
    CSV of `BookOps.huvudbok()`: every kontering grouped per konto with a running saldo,
    and a summary row per konto so the file is readable on its own.
    """
    buf, w = _writer()
    w.writerow(["Konto", "Kontonamn", "Verifikat", "Datum", "Verifikationstext",
                "Radtext", "Debet", "Kredit", "Saldo"])
    for acc in accounts:
        for ln in acc["lines"]:
            amt = ln["amount_ore"]
            w.writerow([
                acc["bas_konto"], acc["konto_namn"],
                ln.get("ver") or "", ln["ver_date"],
                ln.get("ver_text") or "", ln.get("text") or "",
                _kr(amt) if amt > 0 else "", _kr(-amt) if amt < 0 else "",
                _kr(ln["saldo_ore"]),
            ])
        w.writerow([acc["bas_konto"], acc["konto_namn"], "", "",
                    f"Summa {acc['bas_konto']}", "",
                    _kr(acc["debit_ore"]), _kr(acc["credit_ore"]), _kr(acc["saldo_ore"])])
    return BOM + buf.getvalue()


def grundbok_csv(verifikationer: list[dict]) -> str:
    """CSV of `BookOps.verifikationer_full()`: one row per kontering, journal order."""
    buf, w = _writer()
    w.writerow(["Verifikat", "Datum", "Text", "Kvitto/Fakturanr", "Kommentar",
                "Konto", "Kontonamn", "Radtext", "Debet", "Kredit"])
    for v in verifikationer:
        for p in v["postings"]:
            amt = p["amount_ore"]
            w.writerow([
                f"{v['series']}{v['ver_number']}", v["ver_date"], v.get("text") or "",
                v.get("ext_ref") or "", v.get("kommentar") or v.get("motivering") or "",
                p["bas_konto"], p.get("konto_namn") or "", p.get("text") or "",
                _kr(amt) if amt > 0 else "", _kr(-amt) if amt < 0 else "",
            ])
    return BOM + buf.getvalue()


def saldolista_csv(accounts: list[dict]) -> str:
    """A one-row-per-konto balance list (saldobalans) — the quickest sanity check."""
    buf, w = _writer()
    w.writerow(["Konto", "Kontonamn", "Debet", "Kredit", "Saldo"])
    tot_d = tot_k = 0
    for acc in accounts:
        tot_d += acc["debit_ore"]
        tot_k += acc["credit_ore"]
        w.writerow([acc["bas_konto"], acc["konto_namn"], _kr(acc["debit_ore"]),
                    _kr(acc["credit_ore"]), _kr(acc["saldo_ore"])])
    # Debit and credit must be equal because every verifikation balances; a difference
    # here means something is wrong with the books, so it is worth showing.
    w.writerow(["", "Summa", _kr(tot_d), _kr(tot_k), _kr(tot_d - tot_k)])
    return BOM + buf.getvalue()
