"""
Year-end tax estimate for an ENSKILD NÄRINGSIDKARE (the "Skatt"-tab).

Combines the ledger's moms (momsdeklaration) and the year's accounting result (årets
resultat from the förenklat årsbokslut, off the raw posting table) with editable config
rates into an estimate of what to set aside for Skatteverket:

  * MOMS — net utgående − ingående (from the momsdeklaration).
  * EGENAVGIFTER — 28,97 % of the firma's överskott, less the general nedsättning
    (7,5 %, max 15 000 kr); only on the näringsinkomst, never on employment salary.
  * INKOMSTSKATT — kommunal + statlig inkomstskatt, begravnings- and public service-
    avgift, less jobbskatteavdrag and skattereduktion för förvärvsinkomst, computed on
    the TOTAL förvärvsinkomst (firma överskott + any employment salary). Employment tax
    is withheld by the employer, so the firma's own liability is the MARGINAL income tax
    the firma income adds on top of the salary — income_tax(överskott + lön) −
    income_tax(lön). This is why entering the salary matters: it decides which bracket
    (and the jobbskatteavdrag / grundavdrag / statlig-skatt effects) the firma income
    lands in.

This is a HELP / PREVIEW, not a deklaration. The formulas reproduce Skatteverket's
"Räkna ut din skatt" to within a few kronor for 2026, but every annual figure is
editable config — update them each year from Skatteverkets "Belopp och procent" (the
key figures: prisbasbelopp, skiktgräns, egenavgifter, public service-avgift …) and
regeringens "Beräkningskonventioner 20XX" (the exact jobbskatteavdrag / grundavdrag
formulas); verify against Skatteverkets "Räkna ut din skatt". Assumes a person under 66
with ordinary jobbskatteavdrag; the high-income jobbskatteavdrag phase-out is not
modelled. Percentages are centi-percent (28,97 % -> 2897); money is öre.
"""

from __future__ import annotations

import math
import sqlite3

from backend.reports import vat, arsbokslut

# The config keys the estimate reads (all integers), with a UI kind + Swedish label so
# the tab can render an editable form for exactly these annual figures.
CONFIG_FIELDS = [
    ("prisbasbelopp_ore", "ore", "Prisbasbelopp"),
    ("kommunal_skattesats_pct_centi", "pct", "Kommunalskatt %"),
    ("begravningsavgift_pct_centi", "pct", "Begravningsavgift %"),
    ("egenavgift_pct_centi", "pct", "Egenavgifter %"),
    ("egenavgift_nedsattning_pct_centi", "pct", "Nedsättning egenavgifter %"),
    ("egenavgift_nedsattning_max_ore", "ore", "Nedsättning max"),
    ("egenavgift_nedsattning_threshold_ore", "ore", "Nedsättning kräver överskott >"),
    ("public_service_pct_centi", "pct", "Public service-avgift %"),
    ("public_service_max_ore", "ore", "Public service max"),
    ("statlig_skatt_pct_centi", "pct", "Statlig skatt %"),
    ("statlig_skiktgrans_ore", "ore", "Skiktgräns statlig skatt"),
    ("skattered_forvarv_pct_centi", "pct", "Skattered. förvärvsinkomst %"),
    ("skattered_forvarv_floor_ore", "ore", "… på inkomst över"),
    ("skattered_forvarv_max_ore", "ore", "… max"),
    ("ovrig_forvarvsinkomst_ore", "ore", "Övrig förvärvsinkomst (lön m.m.)"),
]
CONFIG_KEYS = [k for k, _, _ in CONFIG_FIELDS]

# Jobbskatteavdrag 2026 (person under 66), Beräkningskonventioner 2026 Tabell 2.10: the
# "belopp" is piecewise in prisbasbelopp; the skattereduktion is (belopp − grundavdrag)
# × kommunalskattesatsen. There is NO high-income phase-out in the 2026 construction.
# Grundavdrag is the standard piecewise schablon (Tabell 2.2). When the year changes,
# refresh these constants from regeringens "Beräkningskonventioner 20XX" (which publishes
# both formulas) — SKV 152 was discontinued 2015.
_JSA_BREAKS = (0.91, 3.24, 8.08)        # bracket edges, in prisbasbelopp
_JSA_C2, _JSA_C3 = 0.3874, 0.251        # slopes in brackets 2 and 3
_JSA_B3_BASE, _JSA_B4_LEVEL = 1.813, 3.027   # belopp levels (PBB) at the start of bracket 3 / in bracket 4


def _config(conn: sqlite3.Connection) -> dict:
    return {k: int(v) for k, v in conn.execute("SELECT key, value FROM config")
            if k in CONFIG_KEYS}


def _grundavdrag(fi_kr: float, pbb: float) -> int:
    """Grundavdrag (schablon), Beräkningskonventioner 2026 Tabell 2.2 — piecewise in
    prisbasbelopp. Fastställd förvärvsinkomst is rounded DOWN to whole 100 kr, grundavdrag
    UP to whole 100 kr."""
    if fi_kr <= 0:
        return 0
    fi_kr = (int(fi_kr) // 100) * 100        # FFI avrundas nedåt till närmaste hundratal
    if fi_kr <= 0.99 * pbb:
        ga = 0.423 * pbb
    elif fi_kr <= 2.72 * pbb:
        ga = 0.423 * pbb + 0.20 * (fi_kr - 0.99 * pbb)
    elif fi_kr <= 3.11 * pbb:
        ga = 0.77 * pbb
    elif fi_kr <= 7.88 * pbb:
        ga = 0.77 * pbb - 0.10 * (fi_kr - 3.11 * pbb)
    else:
        ga = 0.293 * pbb
    ga = min(ga, fi_kr)
    return int(math.ceil(ga / 100.0)) * 100


def _jobbskatteavdrag(ai_kr: float, ga_kr: float, pbb: float, kommunal_frac: float) -> int:
    """Skattereduktion för arbetsinkomster (jobbskatteavdrag), person < 66."""
    if ai_kr <= 0:
        return 0
    b1, b2, b3 = (x * pbb for x in _JSA_BREAKS)
    if ai_kr <= b1:
        belopp = ai_kr
    elif ai_kr <= b2:
        belopp = b1 + _JSA_C2 * (ai_kr - b1)
    elif ai_kr <= b3:
        belopp = _JSA_B3_BASE * pbb + _JSA_C3 * (ai_kr - b2)
    else:
        belopp = _JSA_B4_LEVEL * pbb
    return round(max(0.0, belopp - ga_kr) * kommunal_frac)


def _income_tax(fi_ore: int, c: dict) -> dict:
    """Income tax (kronor→öre) on a förvärvsinkomst: grundavdrag, kommunal + statlig
    skatt, begravnings-/public service-avgift, less jobbskatteavdrag and skattereduktion
    för förvärvsinkomst. EXCLUDES egenavgifter. Allmän pensionsavgift is fully offset by
    its skattereduktion, so it nets to zero and is omitted."""
    pbb = c["prisbasbelopp_ore"] / 100.0
    kommunal_frac = c["kommunal_skattesats_pct_centi"] / 10000.0
    fi = fi_ore / 100.0
    ga = _grundavdrag(fi, pbb)                       # kronor
    besk = max(0.0, fi - ga)                         # beskattningsbar förvärvsinkomst, kr
    kommunal = round(kommunal_frac * besk)
    begravning = int(c["begravningsavgift_pct_centi"] / 10000.0 * besk)
    pubserv = min(round(c["public_service_pct_centi"] / 10000.0 * besk),
                  c["public_service_max_ore"] // 100) if besk > 0 else 0
    skikt = c["statlig_skiktgrans_ore"] / 100.0
    statlig = round(c["statlig_skatt_pct_centi"] / 10000.0 * max(0.0, besk - skikt))
    jsa = _jobbskatteavdrag(fi, ga, pbb, kommunal_frac)
    sr_floor = c["skattered_forvarv_floor_ore"] / 100.0
    skattered = min(int(c["skattered_forvarv_pct_centi"] / 10000.0 * max(0.0, besk - sr_floor)),
                    c["skattered_forvarv_max_ore"] // 100) if besk > 0 else 0
    tax = kommunal + begravning + pubserv + statlig - jsa - skattered
    return {"grundavdrag_ore": ga * 100, "beskattningsbar_ore": int(besk * 100),
            "kommunal_ore": kommunal * 100, "begravning_ore": begravning * 100,
            "public_service_ore": pubserv * 100, "statlig_ore": statlig * 100,
            "jobbskatteavdrag_ore": jsa * 100, "skattered_forvarv_ore": skattered * 100,
            "tax_ore": tax * 100}


def _egenavgifter(overskott_ore: int, c: dict) -> dict:
    """Egenavgifter (net of the general nedsättning) on the firma's överskott, in öre."""
    if overskott_ore <= 0:
        return {"brutto_ore": 0, "nedsattning_ore": 0, "netto_ore": 0}
    P = overskott_ore / 100.0
    brutto = round(c["egenavgift_pct_centi"] / 10000.0 * P)
    neds = 0
    if overskott_ore > c["egenavgift_nedsattning_threshold_ore"]:
        neds = min(round(c["egenavgift_nedsattning_pct_centi"] / 10000.0 * P),
                   c["egenavgift_nedsattning_max_ore"] // 100)
    return {"brutto_ore": brutto * 100, "nedsattning_ore": neds * 100,
            "netto_ore": (brutto - neds) * 100}


def tax_estimate(conn: sqlite3.Connection, fy_start: str, fy_end: str) -> dict:
    """Estimate the tax owed to Skatteverket for the fiscal year [fy_start, fy_end]."""
    c = _config(conn)
    moms_net = vat.momsdeklaration(conn, fy_start, fy_end)["boxes"]["49"]      # >0 owed
    overskott = arsbokslut.forenklat_arsbokslut(conn, fy_start, fy_end)["arets_resultat_ore"]
    salary = c["ovrig_forvarvsinkomst_ore"]

    ea = _egenavgifter(overskott, c)
    it_firma_only = _income_tax(max(0, overskott), c)          # firma income alone
    it_total = _income_tax(max(0, overskott) + salary, c)      # firma + salary
    it_salary = _income_tax(salary, c)                          # salary alone (employer-withheld)

    # The firma's own income-tax liability is the marginal amount its income adds on top
    # of the salary (per-component so the breakdown is transparent).
    def _marg(key):
        return it_total[key] - it_salary[key]
    firma_income_tax = _marg("tax_ore")
    firma_tax_ore = ea["netto_ore"] + firma_income_tax           # excl moms
    firma_total_ore = firma_tax_ore + max(0, moms_net)

    lines = [
        {"key": "moms", "label": "Moms (utgående − ingående)", "amount_ore": moms_net,
         "note": "Redovisas löpande per momsperiod"},
        {"key": "egenavgifter", "label": "Egenavgifter (netto)", "amount_ore": ea["netto_ore"],
         "note": f"28,97 % − nedsättning {_kr(ea['nedsattning_ore'])} kr"},
        {"key": "kommunalskatt", "label": "Kommunal inkomstskatt (firmans del)",
         "amount_ore": _marg("kommunal_ore"), "note": "marginellt ovanpå lönen"},
        {"key": "statlig", "label": "Statlig inkomstskatt (firmans del)",
         "amount_ore": _marg("statlig_ore"), "note": "20 % över skiktgränsen"},
        {"key": "avgifter", "label": "Begravnings- + public service-avgift (firmans del)",
         "amount_ore": _marg("begravning_ore") + _marg("public_service_ore"), "note": ""},
        {"key": "jobbskatteavdrag", "label": "Jobbskatteavdrag (firmans del)",
         "amount_ore": -(_marg("jobbskatteavdrag_ore") + _marg("skattered_forvarv_ore")),
         "note": "skattereduktion (uppskattad)"},
    ]

    return {
        "fiscal_year_start": fy_start, "fiscal_year_end": fy_end,
        "overskott_ore": overskott, "ovrig_forvarvsinkomst_ore": salary,
        "moms_ore": moms_net,
        "egenavgifter": ea,
        "firma_income_tax_ore": firma_income_tax,
        "firma_tax_ore": firma_tax_ore,               # egenavgifter + marginal income tax (excl moms)
        "firma_total_ore": firma_total_ore,           # + moms — what to set aside for the firma
        "lines": lines,
        # Full-picture overview (total förvärvsinkomst) for context.
        "overview": {
            "forvarvsinkomst_ore": (max(0, overskott) + salary),
            "income_tax_total": it_total,
            "income_tax_salary": it_salary,
            "income_tax_firma_only": it_firma_only,
            "total_skatt_ore": it_total["tax_ore"] + ea["netto_ore"],   # firma + salary income tax + egenavg
            "salary_skatt_ore": it_salary["tax_ore"],                   # employer-withheld
        },
        "assumptions": {k: c[k] for k in CONFIG_KEYS},
    }


def _kr(ore: int) -> str:
    return f"{ore / 100:.0f}"
