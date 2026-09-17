"""
vat.py — Momsdeklaration helper (Layer 6, report #1).

Produces the figures for Skatteverket's VAT return for a period. This is the most
frequent report (monthly/quarterly) and falls directly out of the per-rate moms
data recorded on each transaktion.

kontantmetod: moms is reported when money moves, so the period filter is on the
verifikation date (= payment date) of *posted* entries. Pending (unbooked)
transaktioner are correctly excluded.

Boxes produced (domestic small-business subset of the form):

    05  Momspliktig försäljning exkl. moms (beskattningsunderlag)
    10  Utgående moms 25 %
    11  Utgående moms 12 %
    12  Utgående moms 6 %
    20  Inköp av varor från ett annat EU-land
    21  Inköp av tjänster från ett annat EU-land enligt huvudregeln
    22  Inköp av tjänster från ett land utanför EU
    23  Inköp av varor i Sverige (omvänd betalningsskyldighet)
    24  Övriga inköp av tjänster (omvänd betalningsskyldighet)
    30  Utgående moms 25 % på inköpen i ruta 20–24
    31  Utgående moms 12 % på inköpen i ruta 20–24
    32  Utgående moms 6 % på inköpen i ruta 20–24
    48  Ingående moms att dra av
    49  Moms att betala (+) eller få tillbaka (−)
        = (10+11+12) + (30+31+32) − 48

Omvänd betalningsskyldighet (boxes 20–24/30–32) comes from purchase moms_lines marked
with a `reverse_charge` kind: the seller invoiced without moms, so the buyer reports the
underlag in its box, the computed moms as utgående in 30–32, and the same moms as
ingående in 48 — netting to zero with full avdragsrätt.

Plus informational sums for 0 %-rated and momsfri sales. The SALES-side cross-border
boxes (35–41) are still out of scope — the faktura has no EU/export marking yet.

Note: a *rättelse* currently adjusts the ledger postings but does not net the
business-level moms_line aggregation this report reads. Period locking prevents
back-dating into an already-filed period, so the common flow (correct, then file)
is safe; revisit if richer in-period corrections are added.
"""

from __future__ import annotations

import sqlite3

from backend.models.schema import REVERSE_CHARGE_BOXES


def momsdeklaration(conn: sqlite3.Connection, period_start: str, period_end: str) -> dict:
    """
    Compute momsdeklaration figures for [period_start, period_end] (inclusive,
    YYYY-MM-DD). Returns a dict of boxes and detail; all amounts are integer ören.
    """
    rows = conn.execute(
        """
        SELECT t.direction AS direction,
               m.rate_code AS rate_code,
               m.reverse_charge AS reverse_charge,
               SUM(m.ex_moms_ore)  AS ex,
               SUM(m.moms_ore)     AS moms,
               SUM(m.inc_moms_ore) AS inc
        FROM moms_line m
        JOIN transaktion t  ON t.id = m.transaktion_id
        JOIN verifikation v ON v.id = t.verifikation_id
        WHERE v.posted = 1 AND v.ver_date BETWEEN ? AND ?
        GROUP BY t.direction, m.rate_code, m.reverse_charge
        """,
        (period_start, period_end),
    ).fetchall()

    output_vat = {"25": 0, "12": 0, "6": 0}
    reverse_vat = {"25": 0, "12": 0, "6": 0}   # boxes 30/31/32
    reverse_base = {"20": 0, "21": 0, "22": 0, "23": 0, "24": 0}
    sales_base = 0          # box 05
    zero_rated_sales = 0    # informational (0 %)
    momsfri_sales = 0       # informational (momsfri)
    input_vat = 0           # box 48

    for r in rows:
        rate, ex, moms = r["rate_code"], r["ex"], r["moms"]
        if r["direction"] == "out":
            if rate in output_vat:
                output_vat[rate] += moms
                sales_base += ex
            elif rate == "0":
                zero_rated_sales += ex
            else:  # momsfri / ej_avdragsgill (latter unusual on a sale)
                momsfri_sales += ex
        else:  # 'in' — purchases; deductible ingående moms (ej_avdragsgill has moms 0)
            input_vat += moms
            kind = r["reverse_charge"]
            if kind:
                # Omvänd betalningsskyldighet: the underlag goes in its own box and the
                # computed moms is ALSO owed as utgående moms (box 30–32).
                box, _label = REVERSE_CHARGE_BOXES[kind]
                reverse_base[box] += ex
                if rate in reverse_vat:
                    reverse_vat[rate] += moms

    domestic_output = output_vat["25"] + output_vat["12"] + output_vat["6"]
    reverse_output = reverse_vat["25"] + reverse_vat["12"] + reverse_vat["6"]
    output_total = domestic_output + reverse_output
    to_pay = output_total - input_vat

    return {
        "period": {"start": period_start, "end": period_end},
        "boxes": {
            "05": sales_base,
            "10": output_vat["25"],
            "11": output_vat["12"],
            "12": output_vat["6"],
            "20": reverse_base["20"],
            "21": reverse_base["21"],
            "22": reverse_base["22"],
            "23": reverse_base["23"],
            "24": reverse_base["24"],
            "30": reverse_vat["25"],
            "31": reverse_vat["12"],
            "32": reverse_vat["6"],
            "48": input_vat,
            "49": to_pay,
        },
        "output_vat_total_ore": output_total,
        "domestic_output_vat_ore": domestic_output,
        "reverse_charge_vat_ore": reverse_output,
        "reverse_charge_base_ore": sum(reverse_base.values()),
        "input_vat_ore": input_vat,
        "vat_to_pay_ore": to_pay,          # positive = pay; negative = refund
        "sales_base_ore": sales_base,
        "zero_rated_sales_ore": zero_rated_sales,
        "momsfri_sales_ore": momsfri_sales,
    }
