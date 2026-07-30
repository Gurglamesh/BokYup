"""
Tests for Layer 6: reports (momsdeklaration, result/NE building block, SIE export).
"""

from __future__ import annotations

import pytest
from pathlib import Path

from backend.db.manager import DatabaseManager
from backend.db.operations import BookOps
from backend.models import schema as S
from backend.reports import result as result_report_mod
from backend.reports import sie as sie_mod
from backend.reports import vat as vat_mod


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def ops(tmp_path: Path) -> BookOps:
    mgr = DatabaseManager(app_dir=tmp_path / "app")
    _, session = mgr.create_book("Book", str(tmp_path / "book.db"), "pw")
    S.initialize_schema(session.connection())
    return BookOps(session)


def _sale(ops, amount_ore, date, rate="25", cat=None):
    cat = cat or ops.create_category("Försäljning", "income", 3001)
    kid = ops.create_customer("business", company_name="ACME AB")
    return ops.record_income(kid, cat, [{"rate_code": rate, "amount_ore": amount_ore}],
                             date, paid_date=date)


def _purchase(ops, amount_ore, date, rate="25", cat=None):
    cat = cat or ops.create_category("Inköp", "expense", 5460)
    return ops.record_expense(None, cat, [{"rate_code": rate, "amount_ore": amount_ore}],
                              date, paid_date=date)


# ---------------------------------------------------------------------------
# Momsdeklaration
# ---------------------------------------------------------------------------

class TestMomsdeklaration:
    def test_basic_sale_and_purchase(self, ops):
        _sale(ops, 1250, "2026-02-10")       # ex 1000, moms 250 (out)
        _purchase(ops, 625, "2026-02-15")    # ex 500, moms 125 (in)
        rep = vat_mod.momsdeklaration(ops.conn, "2026-01-01", "2026-03-31")
        assert rep["boxes"]["05"] == 1000    # sales base ex-moms
        assert rep["boxes"]["10"] == 250     # utgående 25%
        assert rep["boxes"]["48"] == 125     # ingående
        assert rep["boxes"]["49"] == 125     # 250 - 125 to pay

    def test_multiple_rates(self, ops):
        _sale(ops, 1250, "2026-02-10", rate="25")  # moms 250
        _sale(ops, 1120, "2026-02-11", rate="12")  # ex 1000, moms 120
        rep = vat_mod.momsdeklaration(ops.conn, "2026-01-01", "2026-03-31")
        assert rep["boxes"]["10"] == 250
        assert rep["boxes"]["11"] == 120

    def test_period_filtering_excludes_other_quarters(self, ops):
        _sale(ops, 1250, "2026-02-10")
        _sale(ops, 1250, "2026-05-10")  # Q2 — must be excluded from Q1
        rep = vat_mod.momsdeklaration(ops.conn, "2026-01-01", "2026-03-31")
        assert rep["boxes"]["10"] == 250

    def test_pending_unbooked_excluded(self, ops):
        cat = ops.create_category("Försäljning", "income", 3001)
        kid = ops.create_customer("business", company_name="ACME AB")
        ops.record_income(kid, cat, [{"rate_code": "25", "amount_ore": 1250}],
                          "2026-02-10")  # no paid_date -> pending
        rep = vat_mod.momsdeklaration(ops.conn, "2026-01-01", "2026-03-31")
        assert rep["boxes"]["10"] == 0

    def test_refund_when_input_exceeds_output(self, ops):
        _purchase(ops, 1250, "2026-02-15")  # ingående 250, no sales
        rep = vat_mod.momsdeklaration(ops.conn, "2026-01-01", "2026-03-31")
        assert rep["boxes"]["49"] == -250   # negative => refund

    def test_zero_rated_tracked_separately(self, ops):
        _sale(ops, 1000, "2026-02-10", rate="0")
        rep = vat_mod.momsdeklaration(ops.conn, "2026-01-01", "2026-03-31")
        assert rep["boxes"]["10"] == 0
        assert rep["zero_rated_sales_ore"] == 1000


# ---------------------------------------------------------------------------
# Result report (NE building block)
# ---------------------------------------------------------------------------

class TestResultReport:
    def test_income_expense_result(self, ops):
        _sale(ops, 1250, "2026-02-10")     # ex 1000 income
        _purchase(ops, 625, "2026-02-15")  # ex 500 expense
        rep = result_report_mod.result_report(ops.conn, "2026-01-01", "2026-03-31")
        assert rep["income_ore"] == 1000
        assert rep["expense_ore"] == 500
        assert rep["result_ore"] == 500

    def test_breakdown_by_category(self, ops):
        _sale(ops, 1250, "2026-02-10")
        rep = result_report_mod.result_report(ops.conn, "2026-01-01", "2026-03-31")
        kinds = {row["bas_konto"]: row for row in rep["by_category"]}
        assert kinds[3001]["amount_ore"] == 1000
        assert kinds[3001]["kind"] == "income"


# ---------------------------------------------------------------------------
# SIE export
# ---------------------------------------------------------------------------

class TestSieExport:
    def test_contains_required_headers(self, ops):
        _sale(ops, 1250, "2026-02-10")
        text = sie_mod.export_sie(ops.conn, company_name="Min Firma AB")
        for tag in ("#FLAGGA 0", "#SIETYP 4", "#FORMAT PC8", "#PROGRAM", "#GEN"):
            assert tag in text
        assert '#FNAMN "Min Firma AB"' in text

    def test_accounts_and_verifications_present(self, ops):
        _sale(ops, 1250, "2026-02-10")
        text = sie_mod.export_sie(ops.conn)
        assert "#KONTO 3001" in text
        assert "#KONTO 1930" in text
        assert "#VER A 1 20260210" in text
        assert "#TRANS 1930 {} 12.50" in text     # bank debit, kronor
        assert "#TRANS 3001 {} -10.00" in text    # income credit

    def test_verifications_balanced_in_export(self, ops):
        _sale(ops, 1250, "2026-02-10")
        _purchase(ops, 625, "2026-02-15")
        text = sie_mod.export_sie(ops.conn)
        # Sum every #TRANS amount; the whole journal must net to zero.
        total = 0.0
        for line in text.splitlines():
            if line.strip().startswith("#TRANS"):
                total += float(line.split("{}")[1].split()[0])
        assert round(total, 2) == 0.0

    def test_bytes_encoded_cp437(self, ops):
        _sale(ops, 1250, "2026-02-10")
        raw = sie_mod.export_sie_bytes(ops.conn, company_name="Åäö Firma")
        assert isinstance(raw, bytes)
        assert "Åäö Firma".encode("cp437") in raw

    def test_only_posted_verifikationer_exported(self, ops):
        cat = ops.create_category("Försäljning", "income", 3001)
        kid = ops.create_customer("business", company_name="ACME AB")
        ops.record_income(kid, cat, [{"rate_code": "25", "amount_ore": 1250}],
                          "2026-02-10")  # pending -> no verifikation
        text = sie_mod.export_sie(ops.conn)
        assert "#VER" not in text

    def test_fiscal_year_emits_balances(self, ops):
        _sale(ops, 1250, "2026-02-10")  # bank +12.50, income -10.00, moms -2.50
        text = sie_mod.export_sie(ops.conn, company_name="Min Firma",
                                  fiscal_year_start="2026-01-01", fiscal_year_end="2026-12-31")
        assert "#RAR 0 20260101 20261231" in text
        assert "#UB 0 1930 12.50" in text     # closing bank balance (balance sheet)
        assert "#RES 0 3001 -10.00" in text   # revenue result (credit balance, negative)
        # nothing booked before the year -> no opening balances
        assert "#IB 0" not in text


# ---------------------------------------------------------------------------
# Year-end tax estimate (enskild näringsidkare)
# ---------------------------------------------------------------------------

class TestTaxEstimate:
    # Reproduces Skatteverket's "Räkna ut din skatt" for 2026 (kommunalskatt 30,55 %,
    # begravningsavgift 0,07 %, född 1998) to within a few kronor.
    def _firma(self, ops, overskott_kr, kommunal=3055, begravning=7, salary_ore=0):
        ops.set_tax_config({"kommunal_skattesats_pct_centi": kommunal,
                            "begravningsavgift_pct_centi": begravning,
                            "ovrig_forvarvsinkomst_ore": salary_ore})
        cat = ops.create_category("Försäljning", "income", 3001)
        kid = ops.create_customer("business", company_name="ACME AB")
        ops.record_income(kid, cat, [{"rate_code": "25", "amount_ore": overskott_kr * 100,
                                      "inclusive": False}], "2026-03-01", paid_date="2026-03-01")

    def test_egenavgifter_and_firma_only_matches_skatteverket(self, ops):
        from backend.reports import tax as tax_mod
        self._firma(ops, 100000)                        # firma 100 000, no salary
        est = tax_mod.tax_estimate(ops.conn, "2026-01-01", "2026-12-31")
        assert est["overskott_ore"] == 10000000
        assert est["egenavgifter"]["netto_ore"] == 2147000     # 28 970 − 7 500 = 21 470 kr
        # SKV "beräknad skatt" 30 616 kr (egenavgifter + inkomstskatt) — exact (Beräkningskonventioner 2026)
        assert abs(est["overview"]["total_skatt_ore"] - 3061600) <= 200

    def test_salary_shifts_firma_into_marginal_bracket(self, ops):
        from backend.reports import tax as tax_mod
        self._firma(ops, 100000, salary_ore=46200000)   # + lön 462 000 kr
        est = tax_mod.tax_estimate(ops.conn, "2026-01-01", "2026-12-31")
        # SKV: total (562k) 138 481; lön only 87 628; firma marginal 50 853 (excl moms) — exact
        assert abs(est["overview"]["total_skatt_ore"] - 13848100) <= 200
        assert abs(est["overview"]["salary_skatt_ore"] - 8762800) <= 200
        assert abs(est["firma_tax_ore"] - 5085300) <= 200
        # the firma's marginal income tax is much higher than firma-alone (salary used the
        # grundavdrag + low brackets already)
        assert est["firma_income_tax_ore"] > 2500000

    def test_egenavgifter_only_on_firma_not_salary(self, ops):
        from backend.reports import tax as tax_mod
        self._firma(ops, 100000, salary_ore=46200000)
        est = tax_mod.tax_estimate(ops.conn, "2026-01-01", "2026-12-31")
        assert est["egenavgifter"]["netto_ore"] == 2147000     # unchanged by salary

    def test_jobbskatteavdrag_coefficients_are_config_driven(self, ops):
        from backend.reports import tax as tax_mod
        self._firma(ops, 100000)
        base = tax_mod.tax_estimate(ops.conn, "2026-01-01", "2026-12-31")
        # coefficients persist as config with 2026 defaults (no yearly update required)
        assert base["assumptions"]["jsa_c2_centi"] == 3874
        assert base["assumptions"]["tax_values_year"] == 2026
        # editing a coefficient changes the estimate (bigger slope -> bigger jobbskatteavdrag)
        ops.set_tax_config({"jsa_c2_centi": 5000})
        bumped = tax_mod.tax_estimate(ops.conn, "2026-01-01", "2026-12-31")
        assert bumped["overview"]["total_skatt_ore"] < base["overview"]["total_skatt_ore"]

    def test_loss_year_has_no_income_tax(self, ops):
        from backend.reports import tax as tax_mod
        _sale(ops, 12500, "2026-03-01")            # small income
        _purchase(ops, 125000, "2026-04-01")       # bigger expense -> loss
        est = tax_mod.tax_estimate(ops.conn, "2026-01-01", "2026-12-31")
        assert est["overskott_ore"] < 0
        assert est["egenavgifter"]["netto_ore"] == 0
        assert est["firma_income_tax_ore"] == 0
        assert est["firma_total_ore"] == max(0, est["moms_ore"])   # only moms
