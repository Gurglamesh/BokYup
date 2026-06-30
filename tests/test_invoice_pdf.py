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
