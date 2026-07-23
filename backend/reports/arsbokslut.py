"""
arsbokslut.py — Förenklat årsbokslut (SKV 2150), report building block.

Maps the general ledger (all booked postings) into the boxes of Skatteverket's
"Förenklat årsbokslut för enskilda näringsidkare" (SKV 2150): the balansräkning
(B1–B16) and resultaträkning (R1–R11), plus the U1–U4 upplysningar.

This is a HELP/PREVIEW built from the book's own BAS-konton — it is not filed
(the form says the årsbokslut is kept 7 years, not sent in). Every box lists the
accounts that feed it so the mapping is transparent and verifiable, and the two
summa boxes reconcile automatically because every verifikation balances.

Sign conventions (posting.amount_ore is signed: debit > 0, credit < 0):
  * Assets (B1–B9)            display = saldo            (debit-positive)
  * Equity/liabilities        display = −saldo           (credit-positive)
  * Income (R1–R4)            display = −saldo           (credit-positive)
  * Costs (R5–R10)            display = saldo            (debit-positive)
Balansräkningen use the CUMULATIVE saldo up to the fiscal year end (UB);
resultaträkningen use the movement within the fiscal year.
"""

from __future__ import annotations

import sqlite3

# Balansräkning: (box, lo, hi) inclusive BAS-konto ranges. Checked in order, first
# match wins, so put the narrower "mark (ej avskrivningsbart)" ranges before the
# broader byggnader range. Any 1xxx not matched -> B5; any 2xxx not matched -> B16.
_ASSET_RANGES = [
    ("B1", 1000, 1099),                                   # immateriella
    ("B3", 1130, 1159), ("B3", 1180, 1189),               # mark m.m. ej avskrivningsbart
    ("B2", 1100, 1129), ("B2", 1160, 1179), ("B2", 1190, 1199),  # byggnader/mark
    ("B4", 1200, 1299),                                   # maskiner och inventarier
    ("B5", 1300, 1399),                                   # övriga (finansiella) anl.tillg
    ("B6", 1400, 1499),                                   # varulager
    ("B7", 1500, 1559),                                   # kundfordringar
    ("B8", 1560, 1899),                                   # övriga fordringar
    ("B9", 1900, 1999),                                   # kassa och bank
]
_EK_SKULD_RANGES = [
    ("B10", 2000, 2099),                                  # eget kapital
    ("B11", 2100, 2199),                                  # obeskattade reserver
    ("B13", 2300, 2419),                                  # låneskulder (+ checkkredit)
    ("B15", 2440, 2449),                                  # leverantörsskulder
    ("B14", 2500, 2599),                                  # skatteskulder
    # moms (2600–2669) handled specially by net sign (skuld -> B14 / fordran -> B8)
]
# Resultaträkning ranges (checked in order).
_INCOME_RANGES = [
    ("R2", 3800, 3899),                                   # momsfria/övriga intäkter
    ("R1", 3000, 3799), ("R1", 3900, 3999),               # momspliktig försäljning m.m.
    ("R4", 8000, 8399),                                   # ränteintäkter m.m.
]
_COST_RANGES = [
    ("R5", 4000, 4999),                                   # varor, material och tjänster
    ("R6", 5000, 6999),                                   # övriga externa kostnader
    ("R7", 7000, 7699),                                   # anställd personal
    ("R9", 7810, 7829),                                   # avskrivning byggnader/mark
    ("R10", 7700, 7799), ("R10", 7830, 7899),             # avskrivning maskiner/immat.
    ("R8", 8400, 8999),                                   # räntekostnader m.m.
]

_MOMS_LO, _MOMS_HI = 2600, 2669

_BALANS_LABELS = {
    "B1": "Immateriella anläggningstillgångar",
    "B2": "Byggnader och markanläggningar",
    "B3": "Mark och andra tillgångar som inte får skrivas av",
    "B4": "Maskiner och inventarier",
    "B5": "Övriga anläggningstillgångar",
    "B6": "Varulager",
    "B7": "Kundfordringar",
    "B8": "Övriga fordringar",
    "B9": "Kassa och bank",
    "B10": "Eget kapital",
    "B11": "Obeskattade reserver",
    "B13": "Låneskulder",
    "B14": "Skatteskulder",
    "B15": "Leverantörsskulder",
    "B16": "Övriga skulder",
}
_RESULTAT_LABELS = {
    "R1": "Försäljning och utfört arbete samt övriga momspliktiga intäkter",
    "R2": "Momsfria intäkter",
    "R3": "Bil- och bostadsförmån m.m.",
    "R4": "Ränteintäkter m.m.",
    "R5": "Varor, material och tjänster",
    "R6": "Övriga externa kostnader",
    "R7": "Anställd personal",
    "R8": "Räntekostnader m.m.",
    "R9": "Av- och nedskrivningar av byggnader och markanläggningar",
    "R10": "Av- och nedskrivningar av maskiner och inventarier och immateriella tillgångar",
    "R11": "Bokfört resultat",
}

_ASSET_BOXES = ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9"]
_EK_SKULD_BOXES = ["B10", "B11", "B13", "B14", "B15", "B16"]
_INCOME_BOXES = ["R1", "R2", "R3", "R4"]
_COST_BOXES = ["R5", "R6", "R7", "R8", "R9", "R10"]


def _match(ranges, konto, default):
    for box, lo, hi in ranges:
        if lo <= konto <= hi:
            return box
    return default


def forenklat_arsbokslut(conn: sqlite3.Connection, fy_start: str, fy_end: str) -> dict:
    """
    Build the SKV 2150 boxes for the fiscal year [fy_start, fy_end] (inclusive).
    Balansräkning uses cumulative saldo up to fy_end; resultaträkning the year's
    movement. All amounts integer ören. Every box carries its contributing accounts.
    """
    # Balance-sheet accounts: cumulative saldo up to and including the year end.
    b_rows = conn.execute(
        "SELECT p.bas_konto, a.name, SUM(p.amount_ore) AS saldo "
        "FROM posting p JOIN verifikation v ON v.id = p.verifikation_id "
        "JOIN account a ON a.bas_konto = p.bas_konto "
        "WHERE v.ver_date <= ? AND p.bas_konto < 3000 "
        "GROUP BY p.bas_konto ORDER BY p.bas_konto", (fy_end,)).fetchall()
    # Result accounts: movement within the fiscal year.
    r_rows = conn.execute(
        "SELECT p.bas_konto, a.name, SUM(p.amount_ore) AS saldo "
        "FROM posting p JOIN verifikation v ON v.id = p.verifikation_id "
        "JOIN account a ON a.bas_konto = p.bas_konto "
        "WHERE v.ver_date BETWEEN ? AND ? AND p.bas_konto >= 3000 "
        "GROUP BY p.bas_konto ORDER BY p.bas_konto", (fy_start, fy_end)).fetchall()

    def _box(label_map):
        return {b: {"box": b, "label": label_map[b], "value_ore": 0, "accounts": []}
                for b in label_map}

    balans = _box(_BALANS_LABELS)
    resultat = _box(_RESULTAT_LABELS)
    unmapped = []

    def _add(box_dict, box, konto, name, amount):
        b = box_dict[box]
        b["value_ore"] += amount
        b["accounts"].append({"bas_konto": konto, "name": name, "amount_ore": amount})

    # --- Resultaträkning ---
    for r in r_rows:
        konto, name, saldo = r["bas_konto"], r["name"], r["saldo"] or 0
        if konto < 4000 or (8000 <= konto <= 8399):       # income (incl. ränteintäkter)
            box = _match(_INCOME_RANGES, konto, "R1")
            _add(resultat, box, konto, name, -saldo)       # credit-positive
        else:                                              # costs
            box = _match(_COST_RANGES, konto, "R6")
            _add(resultat, box, konto, name, saldo)        # debit-positive

    income = sum(resultat[b]["value_ore"] for b in _INCOME_BOXES)
    costs = sum(resultat[b]["value_ore"] for b in _COST_BOXES)
    arets_resultat = income - costs
    resultat["R11"]["value_ore"] = arets_resultat

    # --- Balansräkning ---
    moms_net = 0                                           # signed saldo of moms accounts
    moms_accounts = []
    for r in b_rows:
        konto, name, saldo = r["bas_konto"], r["name"], r["saldo"] or 0
        if _MOMS_LO <= konto <= _MOMS_HI:                  # defer moms; place by net sign
            moms_net += saldo
            moms_accounts.append((konto, name, saldo))
            continue
        if konto < 2000:                                   # asset
            box = _match(_ASSET_RANGES, konto, "B5")
            _add(balans, box, konto, name, saldo)          # debit-positive
        else:                                              # equity / liability
            box = _match(_EK_SKULD_RANGES, konto, "B16")
            _add(balans, box, konto, name, -saldo)         # credit-positive

    # Net moms: a credit balance is a skatteskuld (B14); a debit balance a fordran (B8).
    if moms_net:
        moms_box = "B8" if moms_net > 0 else "B14"
        sign = 1 if moms_box == "B8" else -1
        for konto, name, saldo in moms_accounts:
            _add(balans, moms_box, konto, name, sign * saldo)

    # Eget kapital includes the year's result (so the balansräkning reconciles).
    balans["B10"]["value_ore"] += arets_resultat
    balans["B10"]["accounts"].append(
        {"bas_konto": None, "name": "Årets resultat (R11)", "amount_ore": arets_resultat})

    summa_tillgangar = sum(balans[b]["value_ore"] for b in _ASSET_BOXES)
    summa_ek_skulder = sum(balans[b]["value_ore"] for b in _EK_SKULD_BOXES)

    # Upplysningar U1–U4 (obeskattade reserver detail). Rarely booked for an enskild
    # firma — read from accounts if present, else 0.
    def _sum_range(lo, hi):
        return -sum((r["saldo"] or 0) for r in b_rows if lo <= r["bas_konto"] <= hi)
    upplysningar = {
        "U1": {"label": "Periodiseringsfonder", "value_ore": _sum_range(2110, 2149)},
        "U2": {"label": "Expansionsfond", "value_ore": 0},
        "U3": {"label": "Ersättningsfond", "value_ore": 0},
        "U4": {"label": "Insatsemission, skogskonto, upphovsmannakonto m.m.", "value_ore": 0},
    }

    return {
        "fiscal_year_start": fy_start,
        "fiscal_year_end": fy_end,
        "resultat": resultat,
        "balans": balans,
        "arets_resultat_ore": arets_resultat,
        "summa_tillgangar_ore": summa_tillgangar,
        "summa_ek_skulder_ore": summa_ek_skulder,
        "diff_ore": summa_tillgangar - summa_ek_skulder,
        "balanserar": summa_tillgangar == summa_ek_skulder,
        "upplysningar": upplysningar,
        "resultat_order": _INCOME_BOXES + _COST_BOXES + ["R11"],
        "balans_tillgangar_order": _ASSET_BOXES,
        "balans_ek_skulder_order": _EK_SKULD_BOXES,
    }
