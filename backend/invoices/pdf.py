"""
pdf.py — render a compliant Swedish faktura to PDF from `BookOps.get_invoice(...)`.

Uses fpdf2 (pure-Python; runs on PC and on the phone as WebAssembly/Pyodide — verified).
Core Helvetica covers Swedish characters (latin-1: å ä ö é …), so no font embedding.

The layout carries everything Skatteverket requires on a faktura: unique invoice
number + dates, seller name/org.nr/VAT/F-skatt, buyer name/address, article lines with
quantity & unit price, beskattningsunderlag + moms per rate, total — plus the RUT
extras: each household recipient's name + personnummer in its own box with their share
of the skattereduktion, and the resulting amount to pay.
"""

from __future__ import annotations


_RATE_LABEL = {"25": "25%", "12": "12%", "6": "6%", "0": "0%",
               "momsfri": "momsfri", "ej_avdragsgill": "ej avdr."}


def _kr(ore: int) -> str:
    sign = "-" if ore < 0 else ""
    kronor, oren = divmod(abs(int(ore)), 100)
    grouped = f"{kronor:,}".replace(",", " ")              # 1234 -> "1 234" (ASCII space)
    return f"{sign}{grouped},{oren:02d} kr"                # -> "1 234,56 kr"


def _qty(centi: int) -> str:
    if centi % 100 == 0:
        return str(centi // 100)
    return f"{centi / 100:.2f}".replace(".", ",")


def render_invoice_pdf(invoice: dict) -> bytes:
    """Return PDF bytes for the dict returned by BookOps.get_invoice()."""
    from fpdf import FPDF

    seller = invoice.get("seller") or {}
    buyer = invoice.get("buyer") or {}
    lines = invoice.get("lines") or []
    recipients = invoice.get("recipients") or []
    methods = invoice.get("payment_methods") or []

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    W = pdf.w - pdf.l_margin - pdf.r_margin

    def text(x, y, s, size=10, bold=False):
        pdf.set_xy(x, y)
        pdf.set_font("Helvetica", "B" if bold else "", size)
        pdf.cell(0, 5, _s(s))

    # ---- header: title + seller (left) / invoice meta (right) ----------------
    text(pdf.l_margin, 16, "FAKTURA", size=22, bold=True)
    y = 30
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_xy(pdf.l_margin, y); pdf.cell(0, 5, _s(seller.get("name") or "(Säljare)"))
    pdf.set_font("Helvetica", "", 9)
    for label, val in (("Org.nr", seller.get("org_nr")), ("Momsreg.nr", seller.get("vat_nr")),
                       ("", seller.get("address")), ("E-post", seller.get("email")),
                       ("Tel", seller.get("phone"))):
        if val:
            y += 5
            pdf.set_xy(pdf.l_margin, y)
            pdf.cell(0, 5, _s(f"{label}: {val}" if label else str(val)))
    if seller.get("f_skatt"):
        y += 5
        pdf.set_xy(pdf.l_margin, y); pdf.cell(0, 5, _s("Godkänd för F-skatt"))

    # invoice meta box (right)
    mx = pdf.l_margin + W * 0.62
    meta = [("Fakturanr", str(invoice.get("invoice_number") or "")),
            ("Fakturadatum", invoice.get("invoice_date") or ""),
            ("Förfallodatum", invoice.get("due_date") or ""),
            ("Leveransdatum", invoice.get("delivery_date") or "")]
    my = 30
    for label, val in meta:
        if not val:
            continue
        pdf.set_font("Helvetica", "B", 9); pdf.set_xy(mx, my); pdf.cell(35, 5, _s(label))
        pdf.set_font("Helvetica", "", 9); pdf.set_xy(mx + 35, my); pdf.cell(0, 5, _s(val))
        my += 5

    # ---- buyer block ---------------------------------------------------------
    by = max(y, my) + 10
    pdf.set_font("Helvetica", "B", 9); pdf.set_xy(pdf.l_margin, by); pdf.cell(0, 5, _s("Kund"))
    pdf.set_font("Helvetica", "", 10)
    name = (buyer.get("company_name")
            or f"{buyer.get('first_name','')} {buyer.get('last_name','')}".strip())
    rows = [name, buyer.get("address"), buyer.get("org_nr"), buyer.get("email")]
    for r in [x for x in rows if x]:
        by += 5
        pdf.set_xy(pdf.l_margin, by); pdf.cell(0, 5, _s(r))

    # ---- line items table ----------------------------------------------------
    ty = by + 12
    cols = [("Beskrivning", 0.40, "L"), ("Antal", 0.10, "R"), ("À-pris", 0.16, "R"),
            ("Moms", 0.12, "R"), ("Belopp", 0.22, "R")]
    pdf.set_fill_color(235, 238, 242)
    pdf.set_xy(pdf.l_margin, ty); pdf.set_font("Helvetica", "B", 9)
    for title, frac, align in cols:
        pdf.cell(W * frac, 7, _s(title), border="B", align=align, fill=True)
    ty += 7
    pdf.set_font("Helvetica", "", 9)
    for ln in lines:
        qty = f"{_qty(ln['quantity_centi'])} {ln.get('unit') or ''}".strip()
        cells = [(ln["description"], 0.40, "L"), (qty, 0.10, "R"),
                 (_kr(ln["unit_price_ore"]), 0.16, "R"),
                 (_RATE_LABEL.get(ln["rate_code"], ln["rate_code"]), 0.12, "R"),
                 (_kr(ln["ex_moms_ore"]), 0.22, "R")]
        pdf.set_xy(pdf.l_margin, ty)
        for val, frac, align in cells:
            pdf.cell(W * frac, 6, _s(val), border="B", align=align)
        ty += 6

    # ---- moms summary per rate + totals -------------------------------------
    by_rate: dict[str, list[int]] = {}
    for ln in lines:
        ex, mm = by_rate.setdefault(ln["rate_code"], [0, 0])
        by_rate[ln["rate_code"]] = [ex + ln["ex_moms_ore"], mm + ln["moms_ore"]]
    ty += 4
    rx = pdf.l_margin + W * 0.55
    rw = W * 0.45
    for rate, (ex, mm) in sorted(by_rate.items()):
        _kv(pdf, rx, ty, rw, f"Moms {_RATE_LABEL.get(rate, rate)} (underlag {_kr(ex)})", _kr(mm))
        ty += 6
    _kv(pdf, rx, ty, rw, "Summa exkl. moms", _kr(invoice["ex_moms_ore"])); ty += 6
    _kv(pdf, rx, ty, rw, "Summa moms", _kr(invoice["moms_ore"])); ty += 6
    _kv(pdf, rx, ty, rw, "Summa inkl. moms", _kr(invoice["inc_moms_ore"]), bold=True); ty += 8

    # ---- RUT / household tax reduction --------------------------------------
    rut_total = invoice.get("rut_total_ore", 0)
    if rut_total:
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_xy(pdf.l_margin, ty); pdf.cell(0, 6, _s("Husarbete (RUT) - skattereduktion"))
        ty += 7
        pdf.set_font("Helvetica", "", 9)
        for r in recipients:
            box = (f"{r['first_name']} {r['last_name']}   "
                   f"Personnr: {r['personnummer']}")
            pdf.set_xy(pdf.l_margin, ty)
            pdf.cell(W * 0.70, 7, _s(box), border=1)
            pdf.cell(W * 0.30, 7, _s(_kr(r["rut_amount_ore"])), border=1, align="R")
            ty += 7
        _kv(pdf, rx, ty + 2, rw, "Begärd skattereduktion", _kr(rut_total)); ty += 8
        _kv(pdf, rx, ty, rw, "Att betala", _kr(invoice["inc_moms_ore"] - rut_total), bold=True)
        ty += 8
    else:
        _kv(pdf, rx, ty, rw, "Att betala", _kr(invoice["inc_moms_ore"]), bold=True); ty += 8

    # ---- payment methods + terms --------------------------------------------
    ty += 4
    pdf.set_font("Helvetica", "B", 9); pdf.set_xy(pdf.l_margin, ty)
    pdf.cell(0, 5, _s("Betalning")); ty += 5
    pdf.set_font("Helvetica", "", 9)
    for m in methods:
        pdf.set_xy(pdf.l_margin, ty); pdf.cell(0, 5, _s(f"{m['label']}: {m['value']}")); ty += 5
    if invoice.get("payment_terms"):
        pdf.set_xy(pdf.l_margin, ty); pdf.cell(0, 5, _s(f"Betalningsvillkor: {invoice['payment_terms']}")); ty += 5
    if invoice.get("note"):
        pdf.set_xy(pdf.l_margin, ty + 2); pdf.multi_cell(W, 5, _s(invoice["note"]))

    return bytes(pdf.output())


def _kv(pdf, x, y, w, label, value, bold=False):
    pdf.set_font("Helvetica", "B" if bold else "", 9)
    pdf.set_xy(x, y); pdf.cell(w * 0.6, 6, _s(label))
    pdf.set_xy(x + w * 0.6, y); pdf.cell(w * 0.4, 6, _s(value), align="R")


def _s(text) -> str:
    """Core PDF fonts are latin-1; replace anything outside it (rare, e.g. an em
    dash a user pasted) so rendering never raises on a legal document."""
    if text is None:
        return ""
    out = []
    for ch in str(text):
        try:
            ch.encode("latin-1")
            out.append(ch)
        except UnicodeEncodeError:
            out.append("-")
    return "".join(out)
