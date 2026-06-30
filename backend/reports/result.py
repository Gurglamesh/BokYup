"""
result.py — Result report / NE-blankett building block (Layer 6, report #2).

A simple profit/loss for a period: net-of-moms income, net-of-moms expense, and the
result, broken down by category and BAS-konto. This is the building block the
simplified income-tax return (NE-blankett) is assembled from.

Amounts are the ex-moms (net) business figures — moms is a pass-through to the state
and is not income/cost. (For ej_avdragsgill purchases, ex == inc, so the
non-deductible moms correctly stays in the cost, matching the bookkeeping.)

kontantmetod: filtered on the verifikation date of posted entries.
"""

from __future__ import annotations

import sqlite3


def result_report(conn: sqlite3.Connection, period_start: str, period_end: str) -> dict:
    """
    Profit/loss for [period_start, period_end] (inclusive). All amounts integer ören.
    """
    # Income/expense is attributed to each moms_line's own category when set (so a
    # multi-category invoice splits across BAS-konton), falling back to the
    # transaktion's category for plain entries.
    rows = conn.execute(
        """
        SELECT t.direction         AS direction,
               c.id                AS category_id,
               c.name              AS category_name,
               c.kind              AS kind,
               c.bas_konto         AS bas_konto,
               SUM(m.ex_moms_ore)  AS ex
        FROM moms_line m
        JOIN transaktion t  ON t.id = m.transaktion_id
        JOIN verifikation v ON v.id = t.verifikation_id
        LEFT JOIN category c ON c.id = COALESCE(m.category_id, t.category_id)
        WHERE v.posted = 1 AND v.ver_date BETWEEN ? AND ?
        GROUP BY t.direction, COALESCE(m.category_id, t.category_id)
        ORDER BY c.bas_konto
        """,
        (period_start, period_end),
    ).fetchall()

    income_ore = 0
    expense_ore = 0
    by_category = []
    for r in rows:
        amount = r["ex"] or 0
        if r["direction"] == "out":
            income_ore += amount
        else:
            expense_ore += amount
        by_category.append({
            "category_id": r["category_id"],
            "name": r["category_name"],
            "kind": r["kind"],
            "bas_konto": r["bas_konto"],
            "amount_ore": amount,
        })

    return {
        "period": {"start": period_start, "end": period_end},
        "income_ore": income_ore,
        "expense_ore": expense_ore,
        "result_ore": income_ore - expense_ore,
        "by_category": by_category,
    }
