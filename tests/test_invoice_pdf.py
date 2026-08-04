"""Tests for the faktura PDF renderer (backend/invoices/pdf.py)."""

from __future__ import annotations

import pytest
from pathlib import Path

from backend.db.manager import DatabaseManager
from backend.db.operations import BookOps
from backend.invoices.pdf import render_invoice_pdf, _kr, _qty
from backend.models import schema as S


@pytest.fixture()
def ops(tmp_path: Path) -> BookOps:
    mgr = DatabaseManager(app_dir=tmp_path / "app")
    _, session = mgr.create_book("Book", str(tmp_path / "book.db"), "pw")
    S.initialize_schema(session.connection())
    return BookOps(session)


def test_formatters():
    assert _kr(123456) == "1 234,56 kr"
    assert _kr(50) == "0,50 kr"
    assert _qty(200) == "2"
    assert _qty(150) == "1,50"


def test_render_plain_invoice(ops):
    ops.set_company(name="Räksmörgås AB", org_nr="556677-8899", vat_nr="SE556677889901",
                    address="Storgatan 1, Stockholm", f_skatt=1)
    ops.create_payment_method("Swish", "123 456 78 90")
    cat = ops.create_category("Tjänster", "income", 3001)
    kid = ops.create_customer("business", company_name="Köpare AB", org_nr="551122-3344",
                              address="Kungsgatan 5, Göteborg")
    inv = ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-01",
                             due_date="2026-03-31", payment_terms="30 dagar netto",
                             lines=[{"description": "Konsultarvode (åäö)", "quantity_centi": 250,
                                     "unit": "h", "unit_price_ore": 120000, "rate_code": "25"}])
    pdf = render_invoice_pdf(ops.get_invoice(inv["invoice_id"]))
    assert pdf[:4] == b"%PDF" and len(pdf) > 800


def test_render_invoice_with_line_discount(ops):
    # One discounted line + one without: exercises the per-line rabatt sub-line and
    # the "Total rabatt" summary row (red). Non-discounted line shows no rabatt field.
    ops.set_company(name="Firma AB", org_nr="556677-8899")
    cat = ops.create_category("Tjänster", "income", 3001, default_rate_code="25")
    kid = ops.create_customer("business", company_name="Köpare AB")
    inv = ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-01",
        due_date="2026-03-31",
        lines=[{"description": "Konsult", "quantity_centi": 200, "unit_price_ore": 100000,
                "rate_code": "25", "discount_pct_centi": 1500},
               {"description": "Material", "quantity_centi": 100, "unit_price_ore": 50000,
                "rate_code": "25"}])
    full = ops.get_invoice(inv["invoice_id"])
    assert full["ex_moms_ore"] == 170000 + 50000        # line 1 discounted, line 2 not
    assert full["lines"][0]["discount_pct_centi"] == 1500
    pdf = render_invoice_pdf(full)
    assert pdf[:4] == b"%PDF" and len(pdf) > 800


def test_render_ores_rounding(ops):
    # inc 658,75 -> the faktura shows an "Öresavrundning" row and a whole-krona total,
    # while underlag/moms stay exact (per Skatteverket). Uses _round_krona/_pay_block.
    from backend.invoices.pdf import _round_krona
    assert _round_krona(65875) == 65900 and _round_krona(65831) == 65800
    ops.set_company(name="Firma AB", org_nr="556677-8899")
    cat = ops.create_category("Försäljning", "income", 3001, default_rate_code="25")
    kid = ops.create_customer("business", company_name="Köpare AB")
    inv = ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-01",
        due_date="2026-03-31",
        lines=[{"description": "Vara", "quantity_centi": 100, "unit_price_ore": 52700,
                "rate_code": "25"}])
    full = ops.get_invoice(inv["invoice_id"])
    assert full["inc_moms_ore"] == 65875        # not whole kronor
    pdf = render_invoice_pdf(full)
    assert pdf[:4] == b"%PDF" and len(pdf) > 800


def test_render_credit_note(ops):
    ops.set_company(name="Räksmörgås AB", org_nr="556677-8899")
    cat = ops.create_category("Tjänster", "income", 3001)
    kid = ops.create_customer("business", company_name="Köpare AB", org_nr="551122-3344",
                              address="Kungsgatan 5, Göteborg")
    inv = ops.create_invoice(customer_id=kid, category_id=cat, invoice_date="2026-03-01",
                             due_date="2026-03-31",
                             lines=[{"description": "Konsultarvode", "quantity_centi": 100,
                                     "unit_price_ore": 100000, "rate_code": "25"}])
    ops.pay_invoice(inv["invoice_id"], date="2026-03-10")          # book it (kontantmetod)
    res = ops.credit_invoice(inv["invoice_id"], reason="fel pris", date="2026-03-20")
    note = ops.get_credit_note(inv["invoice_id"], res["credit_event_id"])
    # the credit note carries its own number, references the original, and is negative
    assert note["invoice_number"] == res["credit_note_number"]
    assert note["credit_of"] == ops.get_invoice(inv["invoice_id"])["invoice_number"]
    assert note["inc_moms_ore"] == -125000
    assert all(ln["ex_moms_ore"] <= 0 for ln in note["lines"])
    pdf = render_invoice_pdf(note)
    assert pdf[:4] == b"%PDF" and len(pdf) > 800


def test_render_rut_household_split(ops):
    ops.set_company(name="Städ AB", org_nr="556677-8899")
    cat = ops.create_category("Städning", "income", 3001)
    kid = ops.create_customer("private", first_name="Anna", last_name="Svensson",
                              personnummer="811218-9876", address="Hemgatan 3")
    inv = ops.create_invoice(
        customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
        lines=[{"description": "Hemstädning", "quantity_centi": 100, "unit_price_ore": 1000000,
                "rate_code": "25", "reduction_type": "rut"}],
        recipients=[{"first_name": "Anna", "last_name": "Svensson",
                     "personnummer": "811218-9876", "share_pct": 60},
                    {"first_name": "Björn", "last_name": "Svensson",
                     "personnummer": "19811218-9876", "share_pct": 40}])
    pdf = render_invoice_pdf(ops.get_invoice(inv["invoice_id"]))
    assert pdf[:4] == b"%PDF" and len(pdf) > 800


def test_render_rot_reduction(ops):
    ops.set_company(name="Bygg AB", org_nr="556677-8899")
    cat = ops.create_category("Snickeri", "income", 3001)
    kid = ops.create_customer("private", first_name="Per", last_name="Berg",
                              personnummer="811218-9876")
    inv = ops.create_invoice(
        customer_id=kid, category_id=cat, invoice_date="2026-03-01", due_date="2026-03-31",
        lines=[{"description": "Snickeriarbete", "quantity_centi": 100, "unit_price_ore": 1000000,
                "rate_code": "25", "reduction_type": "rot"}],
        recipients=[{"first_name": "Per", "last_name": "Berg",
                     "personnummer": "811218-9876", "share_pct": 100}])
    # ROT 30% incl moms on 1 250 000 = 375 000
    assert inv["rot_total_ore"] == 375000
    pdf = render_invoice_pdf(ops.get_invoice(inv["invoice_id"]))
    assert pdf[:4] == b"%PDF" and len(pdf) > 800


def _pages(pdf_bytes: bytes) -> int:
    # Count page objects without a PDF library (pypdf isn't a project dependency): each
    # page is "/Type /Page" while the page tree is "/Type /Pages" — exclude the latter.
    import re
    return len(re.findall(rb"/Type\s*/Page(?![s])", pdf_bytes))


def _synthetic(lines, **extra):
    inv = {
        "invoice_number": 1001, "invoice_date": "2026-08-04", "due_date": "2026-09-03",
        "seller": {"name": "magIT", "org_nr": "556000-0000", "email": "a@b.se"},
        "buyer": {"first_name": "Test", "last_name": "Kund", "street": "Gata 1",
                  "zip_code": "12345", "city": "Stad"},
        "lines": lines, "recipients": [],
        "payment_methods": [{"label": "Bankgiro", "value": "123-4567"}],
        "ex_moms_ore": sum(l["ex_moms_ore"] for l in lines),
        "moms_ore": sum(l["moms_ore"] for l in lines),
        "inc_moms_ore": sum(l["ex_moms_ore"] + l["moms_ore"] for l in lines),
    }
    inv.update(extra)
    return inv


def test_many_lines_do_not_explode_into_empty_pages():
    """Regression: a long invoice used to spawn ~1 near-empty page PER line because the
    absolute-Y layout fought fpdf's auto page break. It must now paginate tightly."""
    lines = []
    for i in range(40):
        desc = ("Mycket lang artikelbeskrivning med installation, konfiguration och "
                f"dokumentation nummer {i}") if i % 5 == 0 else f"Artikel {i}"
        lines.append({"description": desc, "quantity_centi": 100, "unit": "st",
                      "unit_price_ore": 49900, "rate_code": "25", "ex_moms_ore": 49900,
                      "moms_ore": 12475, "discount_pct_centi": 0, "reduction_type": None})
    pages = _pages(render_invoice_pdf(_synthetic(lines)))
    assert 2 <= pages <= 4, f"40 lines should be a handful of pages, got {pages}"


def test_short_invoice_is_one_page():
    lines = [{"description": "Kort rad", "quantity_centi": 100, "unit": "st",
              "unit_price_ore": 10000, "rate_code": "25", "ex_moms_ore": 10000,
              "moms_ore": 2500, "discount_pct_centi": 0, "reduction_type": None}]
    assert _pages(render_invoice_pdf(_synthetic(lines))) == 1


def test_license_keys_add_a_final_page():
    lines = [{"description": "Programvara", "quantity_centi": 100, "unit": "st",
              "unit_price_ore": 50000, "rate_code": "25", "ex_moms_ore": 50000,
              "moms_ore": 12500, "discount_pct_centi": 0, "reduction_type": None}]
    without = _pages(render_invoice_pdf(_synthetic(lines)))
    withkeys = _pages(render_invoice_pdf(_synthetic(lines, license_keys=["AAAA-BBBB", "CCCC-DDDD"])))
    assert withkeys == without + 1


def test_support_cap_notice_renders():
    lines = [{"description": "Tjänst", "quantity_centi": 100, "unit": "st",
              "unit_price_ore": 50000, "rate_code": "25", "ex_moms_ore": 50000,
              "moms_ore": 12500, "discount_pct_centi": 0, "reduction_type": None}]
    pdf = render_invoice_pdf(_synthetic(lines, support_cap_reached=True))
    assert pdf[:4] == b"%PDF" and len(pdf) > 800


def test_preview_invoice_render_persists_nothing(ops):
    ops.set_company(name="Förhands AB", org_nr="556677-8899")
    cat = ops.create_category("Tjänster", "income", 3001)
    kid = ops.create_customer("private", first_name="Pre", last_name="View")
    render = ops.preview_invoice_render({
        "customer_id": kid, "category_id": cat, "invoice_date": "2026-08-04",
        "due_date": "2026-09-03", "license_keys": ["KEY-1"],
        "lines": [{"description": "Konsult", "quantity_centi": 100, "unit_price_ore": 50000, "rate_code": "25"}]})
    assert render["doc_type"] == "faktura_preview" and render["invoice_number"] is None
    assert render["inc_moms_ore"] == 62500
    pdf = render_invoice_pdf(render)
    assert pdf[:4] == b"%PDF" and len(pdf) > 800
    assert ops.list_invoices() == []            # a preview books/persists nothing
