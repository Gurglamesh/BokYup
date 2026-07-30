"""
Year-end tax estimate for an ENSKILD NÄRINGSIDKARE (the "Skatt"-tab).

Combines the ledger's moms (from the momsdeklaration) and the year's accounting
result (årets resultat from the förenklat årsbokslut, which reads the raw posting
table so manual verifikationer and öresavrundning are included) with editable config
rates into an estimate of what to set aside for Skatteverket, broken down per tax:
moms, egenavgifter, kommunal and statlig inkomstskatt.

This is a HELP / PREVIEW, not a deklaration. The real egenavgifter and inkomstskatt
are reconciled in the INK1 / slutskattebesked (grundavdrag, jobbskatteavdrag and the
final egenavgifter are approximated here). All rates live in config because they change
yearly and vary by kommun — the user must set their own kommunal skattesats and verify
the year's values. Percentages are centi-percent (28,97 % -> 2897); money is öre.
"""

from __future__ import annotations

import sqlite3

from backend.reports import vat, arsbokslut

# The config keys this estimate reads, with their kind ('pct_centi' or 'ore'), so the
# API/UI can enumerate and edit exactly the tax knobs.
CONFIG_KEYS = {
    "egenavgift_pct_centi": "pct_centi",
    "egenavgift_schablon_pct_centi": "pct_centi",
    "kommunal_skattesats_pct_centi": "pct_centi",
    "statlig_skatt_pct_centi": "pct_centi",
    "statlig_brytpunkt_ore": "ore",
    "grundavdrag_ore": "ore",
}


def _config(conn: sqlite3.Connection) -> dict:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM config")}


def tax_estimate(conn: sqlite3.Connection, fy_start: str, fy_end: str) -> dict:
    """Estimate the tax owed to Skatteverket for the fiscal year [fy_start, fy_end]."""
    cfg = _config(conn)
    pct = lambda key: int(cfg.get(key, "0")) / 10000.0     # centi-percent -> fraction
    ore = lambda key: int(cfg.get(key, "0"))

    moms_net = vat.momsdeklaration(conn, fy_start, fy_end)["boxes"]["49"]   # >0 owed, <0 refund
    overskott = arsbokslut.forenklat_arsbokslut(conn, fy_start, fy_end)["arets_resultat_ore"]

    schablon = underlag_ea = egenavgifter = 0
    taxerad = beskattningsbar = kommunal = statlig = 0
    if overskott > 0:
        # NE/INK1 flow: schablonavdrag för egenavgifter reduces both the egenavgifts-
        # underlag and the taxable näringsinkomst; egenavgifter are then charged on the
        # reduced underlag; inkomstskatt on the näringsinkomst less grundavdrag.
        schablon = round(overskott * pct("egenavgift_schablon_pct_centi"))
        underlag_ea = overskott - schablon
        egenavgifter = round(underlag_ea * pct("egenavgift_pct_centi"))
        taxerad = overskott - schablon
        beskattningsbar = max(0, taxerad - ore("grundavdrag_ore"))
        kommunal = round(beskattningsbar * pct("kommunal_skattesats_pct_centi"))
        over_bryt = max(0, taxerad - ore("statlig_brytpunkt_ore"))
        statlig = round(over_bryt * pct("statlig_skatt_pct_centi"))

    inkomstskatt = egenavgifter + kommunal + statlig
    total = inkomstskatt + moms_net                        # signed moms (refund reduces)

    lines = [
        {"key": "moms", "label": "Moms (utgående − ingående)", "amount_ore": moms_net,
         "underlag_ore": None, "pct_centi": None,
         "note": "Redovisas löpande per momsperiod"},
        {"key": "egenavgifter", "label": "Egenavgifter", "amount_ore": egenavgifter,
         "underlag_ore": underlag_ea, "pct_centi": int(cfg["egenavgift_pct_centi"]),
         "note": "På överskottet efter schablonavdrag"},
        {"key": "kommunalskatt", "label": "Kommunal inkomstskatt", "amount_ore": kommunal,
         "underlag_ore": beskattningsbar, "pct_centi": int(cfg["kommunal_skattesats_pct_centi"]),
         "note": "På beskattningsbar inkomst (efter grundavdrag)"},
        {"key": "statlig", "label": "Statlig inkomstskatt", "amount_ore": statlig,
         "underlag_ore": max(0, taxerad - ore("statlig_brytpunkt_ore")),
         "pct_centi": int(cfg["statlig_skatt_pct_centi"]),
         "note": "20 % på inkomst över brytpunkten"},
    ]

    return {
        "fiscal_year_start": fy_start, "fiscal_year_end": fy_end,
        "overskott_ore": overskott,
        "schablonavdrag_ore": schablon,
        "taxerad_inkomst_ore": taxerad,
        "grundavdrag_ore": ore("grundavdrag_ore"),
        "beskattningsbar_inkomst_ore": beskattningsbar,
        "moms_ore": moms_net,
        "egenavgifter_ore": egenavgifter,
        "kommunalskatt_ore": kommunal,
        "statlig_skatt_ore": statlig,
        "inkomstskatt_ore": inkomstskatt,
        "total_ore": total,
        "lines": lines,
        "assumptions": {k: int(cfg[k]) for k in CONFIG_KEYS},
    }
