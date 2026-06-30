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


def render_invoice_pdf(invoice: dict, logo_png: bytes | None = None) -> bytes:
    """Return PDF bytes for the dict returned by BookOps.get_invoice().

    `logo_png` is the book's logo (PNG bytes); when given it is drawn top-right and
    appears on every document.
    """
    from fpdf import FPDF

    seller = invoice.get("seller") or {}
    buyer = invoice.get("buyer") or {}
    lines = invoice.get("lines") or []
    recipients = invoice.get("recipients") or []
    methods = invoice.get("payment_methods") or []
    credit_of = invoice.get("credit_of")
    is_credit = credit_of is not None

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    W = pdf.w - pdf.l_margin - pdf.r_margin

    def text(x, y, s, size=10, bold=False):
        pdf.set_xy(x, y)
        pdf.set_font("Helvetica", "B" if bold else "", size)
        pdf.cell(0, 5, _s(s))

    # ---- logo (top-right, on every document), fit within 45 x 24 mm ----------
    logo_bottom = 12
    if logo_png:
        import io
        from PIL import Image
        iw, ih = Image.open(io.BytesIO(logo_png)).size
        ratio = (iw / ih) if ih else 1
        w = 45.0
        h = w / ratio
        if h > 24:
            h = 24.0
            w = h * ratio
        pdf.image(io.BytesIO(logo_png), x=pdf.w - pdf.r_margin - w, y=12, w=w, h=h)
        logo_bottom = 12 + h

    # ---- header: title (left) + invoice meta (right). Seller goes in the footer.
    text(pdf.l_margin, 16, "KREDITFAKTURA" if is_credit else "FAKTURA", size=22, bold=True)

    mx = pdf.l_margin + W * 0.62
    if is_credit:
        meta = [("Kreditfakturanr", str(invoice.get("invoice_number") or "")),
                ("Avser faktura", str(credit_of)),
                ("Datum", invoice.get("invoice_date") or "")]
    else:
        meta = [("Fakturanr", str(invoice.get("invoice_number") or "")),
                ("Fakturadatum", invoice.get("invoice_date") or ""),
                ("Förfallodatum", invoice.get("due_date") or ""),
                ("Leveransdatum", invoice.get("delivery_date") or "")]
    my = max(28, logo_bottom + 3)
    for label, val in meta:
        if not val:
            continue
        pdf.set_font("Helvetica", "B", 9); pdf.set_xy(mx, my); pdf.cell(35, 5, _s(label))
        pdf.set_font("Helvetica", "", 9); pdf.set_xy(mx + 35, my); pdf.cell(0, 5, _s(val))
        my += 5

    # ---- buyer block (top, over the articles): billing + shipping addresses ---
    buyer_name = (buyer.get("company_name")
                  or f"{buyer.get('first_name', '')} {buyer.get('last_name', '')}".strip())
    biz = []
    if buyer.get("org_nr"):
        biz.append("Org.nr: " + str(buyer["org_nr"]))
    if buyer.get("vat_nr"):
        biz.append("Momsreg.nr: " + str(buyer["vat_nr"]))
    # Prefer structured address parts (street / "zip city" / country) when present.
    if buyer.get("street") or buyer.get("zip_code") or buyer.get("city"):
        locality = " ".join(p for p in (str(buyer.get("zip_code") or "").strip(),
                                        str(buyer.get("city") or "").strip()) if p)
        country = str(buyer.get("country") or "").strip()
        addr_lines = [buyer.get("street"), locality]
        if country and country.lower() not in ("sverige", "sweden"):
            addr_lines.append(country)
    else:
        addr_lines = [buyer.get("address")]
    bill_lines = [buyer_name, *addr_lines, *biz, buyer.get("email")]
    ship = buyer.get("shipping_address")
    ship_lines = [buyer_name, ship] if ship else []

    top = max(my, 30) + 6

    def addr_block(x, heading, lines):
        pdf.set_font("Helvetica", "B", 9); pdf.set_xy(x, top); pdf.cell(0, 5, _s(heading))
        pdf.set_font("Helvetica", "", 10)
        yy = top
        for ln in [v for v in lines if v]:
            yy += 5
            pdf.set_xy(x, yy); pdf.cell(0, 5, _s(ln))
        return yy

    yb = addr_block(pdf.l_margin, "Faktureras till", bill_lines)
    ys = addr_block(pdf.l_margin + W * 0.5, "Leveransadress", ship_lines) if ship_lines else top
    by = max(yb, ys)

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
        desc = ln["description"]
        if ln.get("reduction_type"):
            desc += f"  ({ln['reduction_type'].upper()})"     # mark RUT/ROT eligible lines
        cells = [(desc, 0.40, "L"), (qty, 0.10, "R"),
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

    # ---- RUT / ROT household tax reduction (two separate pots) ----------------
    rut_total = invoice.get("rut_total_ore", 0)
    rot_total = invoice.get("rot_total_ore", 0)
    husavdrag = rut_total + rot_total
    if husavdrag:
        title = "Husarbete - skattereduktion"
        if rut_total and rot_total:
            title += " (RUT + ROT)"
        elif rut_total:
            title += " (RUT)"
        else:
            title += " (ROT)"
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_xy(pdf.l_margin, ty); pdf.cell(0, 6, _s(title))
        ty += 7
        # Column header for the recipient boxes.
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_xy(pdf.l_margin, ty)
        pdf.cell(W * 0.52, 6, _s("Mottagare (personnr)"), border=1)
        pdf.cell(W * 0.10, 6, _s("Andel"), border=1, align="R")
        if rut_total:
            pdf.cell(W * 0.19, 6, _s("RUT"), border=1, align="R")
        if rot_total:
            pdf.cell(W * 0.19, 6, _s("ROT"), border=1, align="R")
        ty += 6
        pdf.set_font("Helvetica", "", 9)
        for r in recipients:
            box = f"{r['first_name']} {r['last_name']}  ({r['personnummer']})"
            share = r.get("share_pct")
            pdf.set_xy(pdf.l_margin, ty)
            pdf.cell(W * 0.52, 7, _s(box), border=1)
            pdf.cell(W * 0.10, 7, _s(f"{share:g} %" if share is not None else ""),
                     border=1, align="R")
            if rut_total:
                pdf.cell(W * 0.19, 7, _s(_kr(r.get("rut_amount_ore", 0))), border=1, align="R")
            if rot_total:
                pdf.cell(W * 0.19, 7, _s(_kr(r.get("rot_amount_ore", 0))), border=1, align="R")
            ty += 7
        ty += 2
        if rut_total:
            _kv(pdf, rx, ty, rw, "Begärd skattereduktion RUT", _kr(rut_total)); ty += 6
        if rot_total:
            _kv(pdf, rx, ty, rw, "Begärd skattereduktion ROT", _kr(rot_total)); ty += 6
        _kv(pdf, rx, ty, rw, "Att betala", _kr(invoice["inc_moms_ore"] - husavdrag), bold=True)
        ty += 8
    else:
        pay_label = "Att återfå" if is_credit else "Att betala"
        pay_amount = -invoice["inc_moms_ore"] if is_credit else invoice["inc_moms_ore"]
        _kv(pdf, rx, ty, rw, pay_label, _kr(pay_amount), bold=True); ty += 8

    # ---- payment methods + terms --------------------------------------------
    ty += 4
    if methods:
        pdf.set_font("Helvetica", "B", 9); pdf.set_xy(pdf.l_margin, ty)
        pdf.cell(0, 5, _s("Betalning")); ty += 5
        pdf.set_font("Helvetica", "", 9)
        for m in methods:
            pdf.set_xy(pdf.l_margin, ty); pdf.cell(0, 5, _s(f"{m['label']}: {m['value']}")); ty += 5
    pdf.set_font("Helvetica", "", 9)
    if invoice.get("payment_terms"):
        pdf.set_xy(pdf.l_margin, ty); pdf.cell(0, 5, _s(f"Betalningsvillkor: {invoice['payment_terms']}")); ty += 5
    if invoice.get("note"):
        pdf.set_xy(pdf.l_margin, ty + 2); pdf.multi_cell(W, 5, _s(invoice["note"])); ty = pdf.get_y()

    # ---- seller block: a footer under the payment methods --------------------
    parts = []
    if seller.get("org_nr"):
        parts.append("Org.nr: " + str(seller["org_nr"]))
    if seller.get("vat_nr"):
        parts.append("Momsreg.nr: " + str(seller["vat_nr"]))
    if seller.get("f_skatt"):
        parts.append("Godkänd för F-skatt")
    contact = " · ".join(x for x in (seller.get("address"), seller.get("email"),
                                     seller.get("phone")) if x)
    fy = max(ty + 8, pdf.h - pdf.b_margin - 16)        # near the page bottom
    pdf.set_draw_color(200, 205, 212)
    pdf.line(pdf.l_margin, fy, pdf.w - pdf.r_margin, fy)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_xy(pdf.l_margin, fy + 2); pdf.cell(0, 5, _s(seller.get("name") or ""))
    pdf.set_font("Helvetica", "", 8)
    if parts:
        pdf.set_xy(pdf.l_margin, fy + 7); pdf.cell(0, 4, _s("   ·   ".join(parts)))
    if contact:
        pdf.set_xy(pdf.l_margin, fy + 11); pdf.cell(0, 4, _s(contact))

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
