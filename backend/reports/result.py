"""
result.py — Result report / NE-blankett building block (Layer 6, report #2).

A profit/loss for a period: net-of-moms income, net-of-moms expense, and the result,
broken down per BAS-konto.

**Basis: the raw `posting` table.** Every verifikation is included — invoices, plain
income/expense, rättelser, year-end accruals, depreciation and hand-written manual
entries alike. That was not always so: this report used to read `moms_line`, i.e. only
business transactions, so a pure verifikation (a depreciation, or anything entered by
hand in Huvudbok) silently never appeared, while `arsbokslut.py` — which has always read
the postings — included it. The books gave two different answers for the same year.
Reading the postings makes the two agree by construction.

Sign convention (as the booking code posts them): an expense/asset konto is debited with
a POSITIVE amount, an income konto is credited with a NEGATIVE one. So income is the
negated sum over 3xxx and expense the plain sum over 4xxx–7xxx.

Only RESULT accounts (3000–8999) are included; 1xxx/2xxx are balance-sheet konton and a
purchase booked there (a capitalised inventarie) is not a cost of the year.

Class 8 (finansiella poster) is split by konto: 83xx–839x är ränteintäkter och liknande
(intäkt), resten av 8xxx är kostnad. kontantmetod/fakturametod makes no difference here —
the period filter is the verifikation date, which is what both methods post on.
"""

from __future__ import annotations

import sqlite3

# Result accounts only. 1xxx/2xxx are balance-sheet konton.
_RESULT_MIN, _RESULT_MAX = 3000, 8999
# Class 3 is revenue. In class 8, the 83xx block is financial INCOME (ränteintäkter,
# utdelningar); the rest of 8xxx is financial cost.
_INCOME_RANGES = ((3000, 3999), (8300, 8399))


def _is_income(konto: int) -> bool:
    return any(lo <= konto <= hi for lo, hi in _INCOME_RANGES)


def result_report(conn: sqlite3.Connection, period_start: str, period_end: str) -> dict:
    """
    Profit/loss for [period_start, period_end] (inclusive). All amounts integer ören.

    `by_category` keeps its name for compatibility but is really one row per BAS-konto —
    the only axis a posting actually carries. `category_id`/`name` are filled in from the
    category that maps to the konto when exactly one does, so a book whose konton are all
    distinct categories reads exactly as before.
    """
    rows = conn.execute(
        """
        SELECT p.bas_konto                      AS bas_konto,
               a.name                           AS konto_namn,
               SUM(p.amount_ore)                AS amount,
               (SELECT c.id FROM category c WHERE c.bas_konto = p.bas_konto
                 ORDER BY c.id LIMIT 1)         AS category_id,
               (SELECT COUNT(*) FROM category c WHERE c.bas_konto = p.bas_konto)
                                                AS category_count,
               (SELECT c.name FROM category c WHERE c.bas_konto = p.bas_konto
                 ORDER BY c.id LIMIT 1)         AS category_name
        FROM posting p
        JOIN verifikation v ON v.id = p.verifikation_id
        LEFT JOIN account a ON a.bas_konto = p.bas_konto
        WHERE v.posted = 1
          AND v.ver_date BETWEEN ? AND ?
          AND p.bas_konto BETWEEN ? AND ?
        GROUP BY p.bas_konto
        ORDER BY p.bas_konto
        """,
        (period_start, period_end, _RESULT_MIN, _RESULT_MAX),
    ).fetchall()

    income_ore = 0
    expense_ore = 0
    by_category = []
    for r in rows:
        konto = r["bas_konto"]
        total = r["amount"] or 0
        if _is_income(konto):
            # Credited, so the stored amount is negative; report it positive.
            amount = -total
            income_ore += amount
            kind = "income"
        else:
            amount = total
            expense_ore += amount
            kind = "expense"
        if amount == 0:
            continue                    # a konto that nets out adds nothing to read
        by_category.append({
            # Only claim a category when exactly one maps to this konto; otherwise the
            # konto's own name is the honest label.
            "category_id": r["category_id"] if r["category_count"] == 1 else None,
            "name": (r["category_name"] if r["category_count"] == 1
                     else (r["konto_namn"] or f"Konto {konto}")),
            "kind": kind,
            "bas_konto": konto,
            "amount_ore": amount,
        })

    return {
        "period": {"start": period_start, "end": period_end},
        "income_ore": income_ore,
        "expense_ore": expense_ore,
        "result_ore": income_ore - expense_ore,
        "by_category": by_category,
    }
