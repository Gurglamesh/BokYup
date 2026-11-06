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

    def test_egenavgifter_uses_schablonavdrag_and_avgiftsunderlag(self, ops):
        from backend.reports import tax as tax_mod
        self._firma(ops, 100000)                        # firma 100 000, no salary
        est = tax_mod.tax_estimate(ops.conn, "2026-01-01", "2026-12-31")
        assert est["overskott_ore"] == 10000000
        # Schablonavdrag för egenavgifter 25 % -> avgiftsunderlag 75 000; BOTH the egenavgift %
        # and the 7,5 % nedsättning apply to that underlag (not the gross överskott).
        assert est["schablonavdrag_ore"] == 2500000
        assert est["avgiftsunderlag_ore"] == 7500000
        assert est["egenavgifter"]["netto_ore"] == 1610300     # (28,97 − 7,5) % × 75 000 = 16 103 kr
        # income tax is computed on the reduced (post-schablon) näringsinkomst — the whole
        # estimate then set-aside (verify against SKV "Räkna ut din skatt" / revisor).
        assert abs(est["overview"]["total_skatt_ore"] - 2050600) <= 300

    def test_egenavgifter_matches_hand_calc_for_odd_overskott(self, ops):
        # The user's real case: 85 273 -> schablon 21 318 -> avgiftsunderlag 63 955 ->
        # (28,97 − 7,5) % × 63 955 = 13 731 kr.
        from backend.reports import tax as tax_mod
        self._firma(ops, 85273)
        est = tax_mod.tax_estimate(ops.conn, "2026-01-01", "2026-12-31")
        assert est["avgiftsunderlag_ore"] == 6395475          # 85 273 − 25 % = 63 954,75 kr
        assert est["egenavgifter"]["netto_ore"] == 1373100    # (28,97 − 7,5) % × underlag = 13 731 kr

    def test_salary_shifts_firma_into_marginal_bracket(self, ops):
        from backend.reports import tax as tax_mod
        self._firma(ops, 100000, salary_ore=46200000)   # + lön 462 000 kr
        est = tax_mod.tax_estimate(ops.conn, "2026-01-01", "2026-12-31")
        # income-tax formula still matches SKV for the salary alone (schablonavdrag never
        # touches employment income): lön 462 000 -> 87 628 kr.
        assert abs(est["overview"]["salary_skatt_ore"] - 8762800) <= 200
        # firma set-aside is now on the post-schablon base (egenavgifter 16 103 + marginal
        # income tax on the reduced näringsinkomst).
        assert abs(est["firma_tax_ore"] - 3783200) <= 300
        # the firma's marginal income tax is still much higher than firma-alone (salary used
        # the grundavdrag + low brackets already)
        assert est["firma_income_tax_ore"] > 2000000

    def test_egenavgifter_only_on_firma_not_salary(self, ops):
        from backend.reports import tax as tax_mod
        self._firma(ops, 100000, salary_ore=46200000)
        est = tax_mod.tax_estimate(ops.conn, "2026-01-01", "2026-12-31")
        assert est["egenavgifter"]["netto_ore"] == 1610300     # unchanged by salary

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


class TestReviewKontroller:
    """
    The mechanical checks in the review export. They are a pure function of the data,
    so each one is driven with a hand-built book — including states the app itself
    cannot produce (an unbalanced verifikation means the file was edited outside BokYup,
    and the reviewer has to be told that first of all).
    """

    @staticmethod
    def _ver(n, postings, **kw):
        v = {"id": n, "series": "A", "ver_number": n, "ver_date": "2026-03-01",
             "registration_date": "2026-03-01", "text": f"Ver {n}", "posted": 1,
             "rattelse_of": None, "egenupprattad": 0, "motivering": None,
             "ext_ref": None, "kommentar": None, "underlag": 1,
             "postings": [{"bas_konto": k, "konto_namn": str(k), "amount_ore": a,
                           "text": None} for k, a in postings]}
        v.update(kw)
        return v

    def _data(self, vers, **kw):
        d = {"verifikationer": vers, "accounts": [], "transaktioner": [],
             "period": {"start": "2026-01-01", "end": "2026-12-31", "filtered": False},
             "bank_konto": 1930, "today": "2026-06-30"}
        d.update(kw)
        return d

    def _levels(self, data):
        from backend.reports import review as review_mod
        return {k["text"]: k["level"] for k in review_mod.kontroller(data)}

    def test_balanced_book_passes(self):
        vers = [self._ver(1, [(5410, 1000), (1930, -1000)]),
                self._ver(2, [(5410, 500), (1930, -500)])]
        texts = self._levels(self._data(vers))
        assert any(t.startswith("Alla 2 verifikationer balanserar") for t in texts)
        assert all(v != "flag" for v in texts.values())

    def test_unbalanced_verifikation_is_flagged_first(self):
        from backend.reports import review as review_mod
        vers = [self._ver(1, [(5410, 1000), (1930, -900)])]
        first = review_mod.kontroller(self._data(vers))[0]
        assert first["level"] == "flag"
        assert "ALLVARLIGT" in first["text"] and "A1 (diff 1,00)" in first["text"]

    def test_gap_in_the_number_series(self):
        vers = [self._ver(1, [(5410, 100), (1930, -100)]),
                self._ver(3, [(5410, 100), (1930, -100)])]
        flags = [t for t, lv in self._levels(self._data(vers)).items() if lv == "flag"]
        assert any("hopp i nummerserien" in t and "A2" in t for t in flags)

    def test_a_date_filter_skips_the_series_check(self):
        """A filtered export is cut at both ends, so its holes are not holes in the book."""
        vers = [self._ver(1, [(5410, 100), (1930, -100)]),
                self._ver(3, [(5410, 100), (1930, -100)])]
        data = self._data(vers)
        data["period"]["filtered"] = True
        texts = self._levels(data)
        assert not any("hopp i nummerserien" in t for t in texts)
        assert any("Nummerserien kontrolleras inte" in t for t in texts)

    def test_missing_underlag_excludes_rattelse_and_egenupprattad(self):
        vers = [
            self._ver(1, [(5410, 100), (1930, -100)], underlag=0),              # flagged
            self._ver(2, [(5410, 100), (1930, -100)], underlag=0, rattelse_of=1),
            self._ver(3, [(5410, 100), (2018, -100)], underlag=0,
                      egenupprattad=1, motivering="Privat dator, marknadsvärde 1 kr"),
        ]
        flags = [t for t, lv in self._levels(self._data(vers)).items() if lv == "flag"]
        miss = [t for t in flags if "saknar bifogat underlag" in t]
        assert len(miss) == 1 and "A1" in miss[0] and "A2" not in miss[0] and "A3" not in miss[0]

    def test_egenupprattad_without_motivering_is_flagged(self):
        vers = [self._ver(1, [(5410, 100), (2018, -100)], egenupprattad=1, motivering="  ")]
        flags = [t for t, lv in self._levels(self._data(vers)).items() if lv == "flag"]
        assert any("utan motivering" in t for t in flags)

    def test_same_ext_ref_twice_is_flagged_as_possible_double_booking(self):
        vers = [self._ver(1, [(5410, 100), (1930, -100)], ext_ref="INET-123"),
                self._ver(2, [(5410, 100), (1930, -100)], ext_ref="inet-123")]
        flags = [t for t, lv in self._levels(self._data(vers)).items() if lv == "flag"]
        assert any("Samma kvitto-/fakturanummer" in t and "A1, A2" in t for t in flags)

    def test_negative_bank_and_future_dates(self):
        vers = [self._ver(1, [(5410, 100), (1930, -100)], ver_date="2026-12-24")]
        accounts = [{"bas_konto": 1930, "konto_namn": "Företagskonto", "debit_ore": 0,
                     "credit_ore": 100, "saldo_ore": -100, "lines": []},
                    {"bas_konto": 5410, "konto_namn": "Förbrukning", "debit_ore": 100,
                     "credit_ore": 0, "saldo_ore": 100, "lines": []}]
        flags = [t for t, lv in self._levels(
            self._data(vers, accounts=accounts)).items() if lv == "flag"]
        assert any("NEGATIVT saldo" in t for t in flags)
        assert any("daterade i framtiden" in t and "A1" in t for t in flags)

    def test_late_bookkeeping_is_informational(self):
        vers = [self._ver(1, [(5410, 100), (1930, -100)],
                          ver_date="2026-01-01", registration_date="2026-06-01")]
        texts = self._levels(self._data(vers))
        late = [t for t, lv in texts.items() if "dagar" in t]
        assert late and texts[late[0]] == "info"
