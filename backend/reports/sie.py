"""
sie.py — SIE export (Layer 6, report #3).

SIE is the Swedish standard accounting interchange format that every accounting
program and revisor accepts. This produces a **SIE type 4** export containing the
chart of accounts (#KONTO) and all posted verifikationer with their balanced
posting lines (#VER / #TRANS) — i.e. the full journal.

The whole schema was designed to map cleanly onto SIE: `account` → #KONTO,
`verifikation` → #VER, `posting` → #TRANS (signed kronor, debit positive).

Encoding: the SIE standard mandates code page 437 (declared as `#FORMAT PC8`). Use
`export_sie_bytes` to get correctly-encoded bytes for a `.se` file; `export_sie`
returns the text.

When a fiscal year is given, opening/closing balances are emitted: #IB/#UB for
balance-sheet accounts (1000–2999) and #RES for result accounts (3000–8999),
computed from the posted journal. (Dimensions/objects remain out of scope.)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from backend import __version__ as APP_VERSION

SIE_ENCODING = "cp437"  # SIE "PC8"


def _sie_date(iso_date: str) -> str:
    """'2026-02-01' -> '20260201'. Pass through if already compact."""
    return iso_date.replace("-", "")


def _kr(amount_ore: int) -> str:
    """Signed kronor with 2 decimals and a dot, as SIE expects. 1250 -> '12.50'."""
    return f"{Decimal(amount_ore) / 100:.2f}"


def _q(text: str | None) -> str:
    """Quote a field for SIE, escaping embedded quotes."""
    return '"' + (text or "").replace('"', '\\"') + '"'


def export_sie(conn: sqlite3.Connection, *, company_name: str = "",
               org_nr: str = "", fiscal_year_start: str | None = None,
               fiscal_year_end: str | None = None,
               program: str = "Bokföring") -> str:
    """
    Build a SIE type 4 export string from the chart of accounts and all posted
    verifikationer. Postings are emitted in kronor (debit positive).
    """
    gen_date = datetime.now(timezone.utc).strftime("%Y%m%d")
    lines: list[str] = [
        "#FLAGGA 0",
        f"#PROGRAM {_q(program)} {_q(APP_VERSION)}",
        "#FORMAT PC8",
        f"#GEN {gen_date}",
        "#SIETYP 4",
        f"#FNAMN {_q(company_name)}",
    ]
    if org_nr:
        lines.append(f"#ORGNR {org_nr}")
    if fiscal_year_start and fiscal_year_end:
        lines.append(f"#RAR 0 {_sie_date(fiscal_year_start)} {_sie_date(fiscal_year_end)}")

    # Chart of accounts (#KONTO)
    accounts = conn.execute("SELECT bas_konto, name FROM account ORDER BY bas_konto").fetchall()
    for a in accounts:
        lines.append(f"#KONTO {a['bas_konto']} {_q(a['name'])}")

    # Opening/closing balances for the fiscal year (year index 0).
    if fiscal_year_start and fiscal_year_end:
        for a in accounts:
            konto = a["bas_konto"]
            if konto < 3000:  # balance-sheet accounts -> #IB / #UB
                ib = _account_balance(conn, konto, end=fiscal_year_start, end_exclusive=True)
                ub = _account_balance(conn, konto, end=fiscal_year_end)
                if ib:
                    lines.append(f"#IB 0 {konto} {_kr(ib)}")
                if ub:
                    lines.append(f"#UB 0 {konto} {_kr(ub)}")
            else:             # result accounts -> #RES (net change over the year)
                res = _account_balance(conn, konto, start=fiscal_year_start, end=fiscal_year_end)
                if res:
                    lines.append(f"#RES 0 {konto} {_kr(res)}")

    # Verifikationer (#VER) with their postings (#TRANS), posted only, in order.
    vers = conn.execute(
        "SELECT id, series, ver_number, ver_date, registration_date, text "
        "FROM verifikation WHERE posted = 1 "
        "ORDER BY series, ver_number"
    ).fetchall()
    for v in vers:
        lines.append(
            f"#VER {v['series']} {v['ver_number']} {_sie_date(v['ver_date'])} "
            f"{_q(v['text'])} {_sie_date(v['registration_date'])}"
        )
        lines.append("{")
        postings = conn.execute(
            "SELECT bas_konto, amount_ore, text FROM posting "
            "WHERE verifikation_id = ? ORDER BY id",
            (v["id"],),
        ).fetchall()
        for p in postings:
            trans = f"   #TRANS {p['bas_konto']} {{}} {_kr(p['amount_ore'])}"
            if p["text"]:
                trans += f" {_sie_date(v['ver_date'])} {_q(p['text'])}"
            lines.append(trans)
        lines.append("}")

    return "\n".join(lines) + "\n"


def _account_balance(conn: sqlite3.Connection, konto: int, *, start: str | None = None,
                     end: str | None = None, end_exclusive: bool = False) -> int:
    """Signed öre balance (debit positive) of postings to an account over a date range."""
    q = ("SELECT COALESCE(SUM(p.amount_ore), 0) FROM posting p "
         "JOIN verifikation v ON v.id = p.verifikation_id "
         "WHERE p.bas_konto = ? AND v.posted = 1")
    params: list = [konto]
    if start:
        q += " AND v.ver_date >= ?"
        params.append(start)
    if end:
        q += " AND v.ver_date < ?" if end_exclusive else " AND v.ver_date <= ?"
        params.append(end)
    return conn.execute(q, params).fetchone()[0]


def export_sie_bytes(conn: sqlite3.Connection, **kwargs) -> bytes:
    """Same as export_sie but encoded as cp437 (PC8) for writing a .se file."""
    return export_sie(conn, **kwargs).encode(SIE_ENCODING, errors="replace")
