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


def _round_krona(ore: int) -> int:
    """Öresavrundning (avrundningslagen): to the nearest whole krona, 1–49 öre down,
    50–99 up. Mirrors backend.db.operations._round_to_krona (kept local so the PDF
    renderer stays import-light under Pyodide)."""
    if ore >= 0:
        return ((ore + 50) // 100) * 100
    return -(((-ore + 50) // 100) * 100)


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
    is_offert = invoice.get("doc_type") == "offert"
    is_preview = invoice.get("doc_type") == "faktura_preview"

    pdf = FPDF(format="A4", unit="mm")
    # We lay the whole document out with absolute Y positions, so we drive page breaks
    # ourselves (auto-break fights manual set_xy and spawns near-empty pages).
    pdf.set_auto_page_break(auto=False, margin=18)
    pdf.add_page()
    W = pdf.w - pdf.l_margin - pdf.r_margin

    def text(x, y, s, size=10, bold=False):
        pdf.set_xy(x, y)
        pdf.set_font("Helvetica", "B" if bold else "", size)
        pdf.cell(0, 5, _s(s))

    # ---- header, top-LEFT: just the logo (no big heading) ----
    logo_bottom = 12
    if logo_png:
        import io
        from PIL import Image
        iw, ih = Image.open(io.BytesIO(logo_png)).size
        ratio = (iw / ih) if ih else 1
        w = 55.0
        h = w / ratio
        if h > 24:
            h = 24.0
            w = h * ratio
        pdf.image(io.BytesIO(logo_png), x=pdf.l_margin, y=12, w=w, h=h)
        logo_bottom = 12 + h
    # No big "FAKTURA" heading (Skatteverket doesn't require it, Inet-style) — the
    # document type is identified by the meta label on the right (Fakturanr / Offertnr /
    # Kreditfakturanr).
    title_bottom = logo_bottom
    if is_preview:
        pdf.set_text_color(200, 0, 0)
        text(pdf.l_margin, logo_bottom + 3, "FÖRHANDSVISNING - ej bokförd, inget fakturanummer", size=10, bold=True)
        pdf.set_text_color(0, 0, 0)
        title_bottom = logo_bottom + 9

    # ---- header, top-RIGHT: the document meta (number, dates, references) ----
    if is_offert:
        meta = [("Offertnr", str(invoice.get("invoice_number") or "")),
                ("Offertdatum", invoice.get("invoice_date") or ""),
                ("Giltig till", invoice.get("valid_until") or "")]
    elif is_credit:
        meta = [("Kreditfakturanr", str(invoice.get("invoice_number") or "")),
                ("Avser faktura", str(credit_of)),
                ("Datum", invoice.get("invoice_date") or "")]
    else:
        meta = [("Fakturanr", str(invoice.get("invoice_number") or "")),
                ("Fakturadatum", invoice.get("invoice_date") or ""),
                ("Förfallodatum", invoice.get("due_date") or ""),
                ("Leveransdatum", invoice.get("delivery_date") or ""),
                ("Er referens", invoice.get("your_reference") or ""),
                ("Vår referens", invoice.get("our_reference") or "")]
    mx = pdf.l_margin + W * 0.60
    my = 12
    for label, val in meta:
        if not val:
            continue
        pdf.set_font("Helvetica", "B", 9); pdf.set_xy(mx, my); pdf.cell(33, 5, _s(label))
        pdf.set_font("Helvetica", "", 9); pdf.set_xy(mx + 33, my); pdf.cell(0, 5, _s(val))
        my += 5
    meta_bottom = my

    # ---- buyer name + billing lines ----
    buyer_name = (buyer.get("company_name")
                  or f"{buyer.get('first_name', '')} {buyer.get('last_name', '')}".strip())
    # Optional contact person under a business buyer: show "Att: Förnamn Efternamn"
    # (only the name; the rest of the details are the company's).
    contact_name = (f"{buyer.get('contact_first_name') or ''} "
                    f"{buyer.get('contact_last_name') or ''}".strip()
                    or (buyer.get("contact_person") if buyer.get("company_name") else ""))
    contact_line = f"Att: {contact_name}" if contact_name else None
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
    bill_lines = [buyer_name, contact_line, *addr_lines, *biz,
                  buyer.get("email"), buyer.get("phone")]

    # ---- delivery block (per-invoice, full fields). If none given, the billing
    # address IS the delivery address. Falls back to a legacy single-line
    # shipping_address on older invoices. ----
    deliv = invoice.get("delivery_address") or {}
    deliv_keys = ("name", "street", "zip_code", "city", "org_nr", "vat_nr", "email", "phone")
    if any(str(deliv.get(k) or "").strip() for k in deliv_keys):
        dloc = " ".join(p for p in (str(deliv.get("zip_code") or "").strip(),
                                    str(deliv.get("city") or "").strip()) if p)
        dbiz = []
        if deliv.get("org_nr"):
            dbiz.append("Org.nr: " + str(deliv["org_nr"]))
        if deliv.get("vat_nr"):
            dbiz.append("Momsreg.nr: " + str(deliv["vat_nr"]))
        delivery_lines = [deliv.get("name") or buyer_name, deliv.get("street"), dloc,
                          *dbiz, deliv.get("email"), deliv.get("phone")]
    elif buyer.get("shipping_address"):
        delivery_lines = [buyer_name, buyer.get("shipping_address")]
    else:
        delivery_lines = bill_lines           # ingen egen leveransadress => = faktureringsadress

    # ---- address blocks: Faktureras till (left) + Leveransadress (far right) ----
    top = max(title_bottom, meta_bottom, 40) + 6

    def addr_block(x, heading, lines):
        pdf.set_font("Helvetica", "B", 9); pdf.set_xy(x, top); pdf.cell(0, 5, _s(heading))
        pdf.set_font("Helvetica", "", 10)
        yy = top
        for ln in [v for v in lines if v]:
            yy += 5
            pdf.set_xy(x, yy); pdf.cell(0, 5, _s(ln))
        return yy

    yb = addr_block(pdf.l_margin, "Faktureras till", bill_lines)
    ys = addr_block(pdf.l_margin + W * 0.55, "Leveransadress", delivery_lines)
    by = max(yb, ys)

    # ---- line items table (page-break aware, wrapped descriptions) -----------
    ty = by + 12
    cols = [("Beskrivning", 0.34, "L"), ("Antal", 0.08, "R"), ("À-pris", 0.14, "R"),
            ("Moms", 0.10, "R"), ("Belopp", 0.17, "R"), ("Inkl. moms", 0.17, "R")]
    bottom = pdf.h - pdf.b_margin
    line_h = 4.5

    def header_row(y):
        pdf.set_fill_color(235, 238, 242)
        pdf.set_xy(pdf.l_margin, y); pdf.set_font("Helvetica", "B", 9)
        for title, frac, align in cols:
            pdf.cell(W * frac, 7, _s(title), border="B", align=align, fill=True)
        return y + 7

    def new_page(needed, header=False):
        """Start a fresh page (resetting ty to the top margin) if `needed` mm won't fit
        below the current ty; optionally redraw the line-items header on the new page."""
        nonlocal ty
        if ty + needed > bottom:
            pdf.add_page(); ty = pdf.t_margin
            if header:
                ty = header_row(ty)

    def flow_text(s, w, size, lh, style="", indent=0.0):
        """Word-wrapped, page-break-aware paragraph flow (replaces multi_cell so it can
        cross a page boundary cleanly)."""
        nonlocal ty
        for para in str(s).split("\n"):
            for dl in _wrap(pdf, para, w - indent, size, style):
                new_page(lh)
                pdf.set_font("Helvetica", style, size)
                pdf.set_xy(pdf.l_margin + indent, ty); pdf.cell(0, lh, _s(dl)); ty += lh

    ty = header_row(ty)
    pdf.set_font("Helvetica", "", 9)
    for ln in lines:
        qty = f"{_qty(ln['quantity_centi'])} {ln.get('unit') or ''}".strip()
        desc = ln["description"]
        if ln.get("reduction_type"):
            desc += f"  ({ln['reduction_type'].upper()})"     # mark RUT/ROT eligible lines
        # Rabatt is a program-internal tool that NEVER shows on the document: the à-pris
        # printed here is the DISCOUNTED unit price (net line total ÷ antal), so à-pris ×
        # antal equals Belopp exactly. A non-discounted line is unchanged (net == list).
        qcenti = ln["quantity_centi"]
        unit_ore = round(ln["ex_moms_ore"] * 100 / qcenti) if qcenti else ln["unit_price_ore"]
        desc_lines = _wrap(pdf, desc, W * 0.34 - 2, 9)        # wrap into the Beskrivning column
        row_h = max(6.0, len(desc_lines) * line_h + 1.5)
        new_page(row_h, header=True)
        # Numeric columns: one cell each spanning the whole (possibly multi-line) row.
        # Belopp is the line total ex moms; Inkl. moms is that same line incl. its moms.
        line_inc = ln["ex_moms_ore"] + ln["moms_ore"]
        pdf.set_font("Helvetica", "", 9)
        pdf.set_xy(pdf.l_margin + W * 0.34, ty)
        pdf.cell(W * 0.08, row_h, _s(qty), border="B", align="R")
        pdf.cell(W * 0.14, row_h, _s(_kr(unit_ore)), border="B", align="R")
        pdf.cell(W * 0.10, row_h, _s(_RATE_LABEL.get(ln["rate_code"], ln["rate_code"])), border="B", align="R")
        pdf.cell(W * 0.17, row_h, _s(_kr(ln["ex_moms_ore"])), border="B", align="R")
        pdf.cell(W * 0.17, row_h, _s(_kr(line_inc)), border="B", align="R")
        # Description column: an empty bottom-bordered cell with the wrapped lines on top.
        pdf.set_xy(pdf.l_margin, ty); pdf.cell(W * 0.34, row_h, "", border="B")
        for i, dl in enumerate(desc_lines):
            pdf.set_xy(pdf.l_margin + W * 0.005, ty + 1 + i * line_h)
            pdf.cell(W * 0.34, line_h, _s(dl))
        ty += row_h

    # ---- moms summary per rate + totals -------------------------------------
    by_rate: dict[str, list[int]] = {}
    for ln in lines:
        ex, mm = by_rate.setdefault(ln["rate_code"], [0, 0])
        by_rate[ln["rate_code"]] = [ex + ln["ex_moms_ore"], mm + ln["moms_ore"]]
    ty += 4
    rx = pdf.l_margin + W * 0.55
    rw = W * 0.45
    rut_total = invoice.get("rut_total_ore", 0)
    rot_total = invoice.get("rot_total_ore", 0)
    husavdrag = rut_total + rot_total
    # Keep the WHOLE summary (rabatt + moms + totals + RUT-tabell + Att betala) together on
    # one page: reserve its estimated height so it moves to a fresh page as a unit rather
    # than splitting across the page break. A larger gap at the bottom of the previous page
    # is preferable to a summary that is cut in half.
    summary_h = 6 * len(by_rate) + 20
    summary_h += (21 + 7 * len(recipients) + (6 if rut_total else 0)
                  + (6 if rot_total else 0) + 18) if husavdrag else 18
    if is_offert:
        summary_h += 12
    page_h = pdf.h - pdf.t_margin - pdf.b_margin - 2
    new_page(min(summary_h, page_h))
    for rate, (ex, mm) in sorted(by_rate.items()):
        new_page(6)
        _kv(pdf, rx, ty, rw, f"Moms {_RATE_LABEL.get(rate, rate)} (underlag {_kr(ex)})", _kr(mm))
        ty += 6
    new_page(20)
    _kv(pdf, rx, ty, rw, "Summa exkl. moms", _kr(invoice["ex_moms_ore"])); ty += 6
    _kv(pdf, rx, ty, rw, "Summa moms", _kr(invoice["moms_ore"])); ty += 6
    _kv(pdf, rx, ty, rw, "Summa inkl. moms", _kr(invoice["inc_moms_ore"]), bold=True); ty += 8

    # ---- RUT / ROT household tax reduction (two separate pots) ----------------
    if husavdrag:
        title = "Husarbete - skattereduktion"
        if rut_total and rot_total:
            title += " (RUT + ROT)"
        elif rut_total:
            title += " (RUT)"
        else:
            title += " (ROT)"
        new_page(13 + 7 * len(recipients))
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
            new_page(7)
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
            new_page(6); _kv(pdf, rx, ty, rw, "Begärd skattereduktion RUT", _kr(rut_total)); ty += 6
        if rot_total:
            new_page(6); _kv(pdf, rx, ty, rw, "Begärd skattereduktion ROT", _kr(rot_total)); ty += 6
        new_page(16)
        ty = _pay_block(pdf, rx, ty, rw, "Uppskattat pris" if is_offert else "Att betala",
                        invoice["inc_moms_ore"] - husavdrag)
    else:
        pay_label = "Uppskattat pris" if is_offert else ("Att återfå" if is_credit else "Att betala")
        pay_amount = -invoice["inc_moms_ore"] if is_credit else invoice["inc_moms_ore"]
        new_page(16)
        ty = _pay_block(pdf, rx, ty, rw, pay_label, pay_amount)
    if is_offert:
        ty += 4
        flow_text("Detta är en offert och inte en faktura. Priserna är "
                  "preliminära och giltiga till angivet datum.", W, 8, 4, style="I")

    # ---- note (invoice body, stays with the result) -------------------------
    if invoice.get("note"):
        ty += 2
        flow_text(invoice["note"], W, 9, 5)

    # ---- bottom block: payment methods + terms and the seller footer, kept together
    # and pinned to the BOTTOM of the page, so the payment info is always aligned right
    # above the footer — even when the articles fill the page. Structured company name /
    # address / postnr ort / tax / contact. Support notice + licence keys go on the NEXT
    # page(s) instead of wedging between the payment methods and the footer. ----
    pay_lines = []
    if methods:
        pay_lines.append(("Ange fakturanummer vid betalning:", "B"))
        pay_lines += [(f"{m['label']}: {m['value']}", "") for m in methods]
    if invoice.get("payment_terms"):
        pay_lines.append((f"Betalningsvillkor: {invoice['payment_terms']}", ""))

    tax = []
    if seller.get("org_nr"):
        tax.append("Org.nr: " + str(seller["org_nr"]))
    if seller.get("vat_nr"):
        tax.append("Momsreg.nr: " + str(seller["vat_nr"]))
    if seller.get("f_skatt"):
        tax.append("Godkänd för F-skatt")
    contact = " · ".join(x for x in (seller.get("email"), seller.get("phone")) if x)
    if seller.get("street") or seller.get("zip_code") or seller.get("city"):
        loc = " ".join(p for p in (str(seller.get("zip_code") or "").strip(),
                                   str(seller.get("city") or "").strip()) if p)
        addr_lines = [seller.get("street"), loc]
    else:
        addr_lines = [seller.get("address")]
    foot = [t for t in [*addr_lines, "   ·   ".join(tax), contact] if t]

    pay_h = (5 * len(pay_lines) + 4) if pay_lines else 0
    foot_h = 7 + 4 * len(foot)                         # rule + name + address/tax/contact
    block_h = pay_h + foot_h
    new_page(block_h + 4)                              # move the whole block to a new page if needed
    block_top = max(ty + 6, pdf.h - pdf.b_margin - block_h)   # anchor to the bottom
    yy = block_top
    for txt, style in pay_lines:
        pdf.set_font("Helvetica", style, 9)
        pdf.set_xy(pdf.l_margin, yy); pdf.cell(0, 5, _s(txt)); yy += 5
    fy = yy + (4 if pay_lines else 0)                  # footer rule just under the payment lines
    pdf.set_draw_color(200, 205, 212)
    pdf.line(pdf.l_margin, fy, pdf.w - pdf.r_margin, fy)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_xy(pdf.l_margin, fy + 2); pdf.cell(0, 5, _s(seller.get("name") or ""))
    yy = fy + 7
    for txt in foot:
        pdf.set_font("Helvetica", "", 8)
        pdf.set_xy(pdf.l_margin, yy); pdf.cell(0, 4, _s(txt)); yy += 4
    ty = yy

    # ---- appendix on a NEW page: gratis distanssupport + licence keys --------
    support_on = invoice.get("support_enabled", True)
    show_support = support_on and (invoice.get("support_cap_reached")
                                   or invoice.get("support_expiry_date"))
    keys = [k for k in (invoice.get("license_keys") or []) if str(k).strip()]
    if show_support:
        pdf.add_page(); ty = pdf.t_margin
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_xy(pdf.l_margin, ty); pdf.cell(0, 5, _s("Gratis distanssupport")); ty += 5
        flow_text(_support_cap_text() if invoice.get("support_cap_reached")
                  else _support_text(invoice.get("support_minutes_earned") or 0,
                                     invoice["support_expiry_date"]), W, 8, 4)
    if keys:
        pdf.add_page(); ty = pdf.t_margin
        pdf.set_font("Helvetica", "B", 14)
        pdf.set_xy(pdf.l_margin, ty); pdf.cell(0, 8, _s("Licensnycklar")); ty += 10
        pdf.set_font("Helvetica", "", 9)
        pdf.set_xy(pdf.l_margin, ty)
        pdf.cell(0, 5, _s(f"Tillhör faktura {invoice.get('invoice_number') or ''}.")); ty += 8
        for k in keys:
            new_page(7)
            pdf.set_font("Helvetica", "", 11)
            pdf.set_xy(pdf.l_margin, ty)
            pdf.cell(0, 6, _s("• " + str(k).strip()), border="B"); ty += 7

    return bytes(pdf.output())


def _support_text(minutes: int, expiry_date: str) -> str:
    """The 'gratis distanssupport' block, with the earned minutes + expiry inserted."""
    return (
        "Som betald kund hos magIT erbjuds du kostnadsfri enkel support på distans. "
        "Supporttiden beräknas som 15 minuter för varje fullt 1000-kronorsbelopp av "
        "fakturerat belopp (överskjutande belopp under 1 000 kr ger ingen ytterligare tid), "
        "giltigt i 36 månader från fakturadatum. Tiden räknas per påbörjade 15 minuter vid "
        "uttag och kan nyttjas vid flera tillfällen tills tillgänglig tid är förbrukad eller "
        "giltighetstiden löpt ut. Erbjudandet gäller enklare support (felsökning, rådgivning, "
        "mindre justeringar) och omfattar inte mer omfattande arbete eller besök på plats. "
        "Erbjudandet upphör i förtid endast om verksamheten upphör (t.ex. vid konkurs). "
        f"Denna faktura ger: {minutes} minuter distanssupport, giltigt till {expiry_date}. "
        "Kontakta mig för att få uppgift om hur mycket supporttid du har kvar totalt."
    )


def _support_cap_text() -> str:
    """Shown instead of the earned-time block once a customer has reached the support cap."""
    return (
        "Du har uppnått den maximala gränsen för kostnadsfri distanssupport (12 timmar) "
        "hos magIT. Du är fortfarande varmt välkommen att höra av dig om du har några "
        "problem eller frågor, så hittar vi en lösning tillsammans."
    )


def _wrap(pdf, s, width, size, style=""):
    """Word-wrap `s` to `width` mm at the given Helvetica font; returns a list of line
    strings. Hard-splits any single token wider than the column so nothing overflows
    into the next column. Sets the font as a side effect (callers re-set as needed)."""
    pdf.set_font("Helvetica", style, size)
    s = _s(s)
    if not s.strip():
        return [""]

    def fit(tok):                                       # hard-split an over-wide token
        if pdf.get_string_width(tok) <= width or not tok:
            return [tok]
        parts, cur = [], ""
        for ch in tok:
            if not cur or pdf.get_string_width(cur + ch) <= width:
                cur += ch
            else:
                parts.append(cur); cur = ch
        if cur:
            parts.append(cur)
        return parts

    lines, cur = [], ""
    for word in s.split():
        for piece in fit(word):
            if not cur:
                cur = piece
            elif pdf.get_string_width(cur + " " + piece) <= width:
                cur += " " + piece
            else:
                lines.append(cur); cur = piece
    if cur:
        lines.append(cur)
    return lines or [""]


def _kv(pdf, x, y, w, label, value, bold=False):
    pdf.set_font("Helvetica", "B" if bold else "", 9)
    pdf.set_xy(x, y); pdf.cell(w * 0.6, 6, _s(label))
    pdf.set_xy(x + w * 0.6, y); pdf.cell(w * 0.4, 6, _s(value), align="R")


def _pay_block(pdf, rx, ty, rw, label, exact_ore):
    """Render the summa att betala with öresavrundning: if the exact amount is not
    already whole kronor, show a separate "Öresavrundning" row (the moms/underlag are
    NOT rounded — per Skatteverket's ställningstagande) and the rounded total."""
    rounded = _round_krona(exact_ore)
    diff = rounded - exact_ore
    if diff:
        _kv(pdf, rx, ty, rw, "Öresavrundning", _kr(diff)); ty += 6
    _kv(pdf, rx, ty, rw, label, _kr(rounded), bold=True)
    return ty + 8


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
