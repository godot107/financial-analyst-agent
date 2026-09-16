"""Step 2: the ratios.

The expected values below are typed from Microsoft's FY2026 10-K statements
(in millions of dollars), not copied from our own extraction, so these tests
check the whole path from filing to ratio.
"""

from pathlib import Path

import pytest

from fin_analyst.edgar import Fact, load_facts
from fin_analyst.metrics import METRICS, METRICS_BY_ID, compute_all, describe_metrics

FIXTURE = Path(__file__).parent / "fixtures" / "msft_facts.json"

# Microsoft's FY2026 10-K, year ended 2026-06-30, $ millions.
REVENUE = 331_839
GROSS_PROFIT = 225_465
NET_INCOME = 133_749
OPERATING_CASH_FLOW = 182_935
TOTAL_ASSETS = 758_376
CURRENT_ASSETS = 207_710
CURRENT_LIABILITIES = 168_825
EQUITY = 442_387
CASH = 20_935
SHORT_TERM_INVESTMENTS = 55_908
RECEIVABLES = 80_876
CURRENT_LONG_TERM_DEBT = 9_227
LONG_TERM_DEBT = 31_067
SHORT_TERM_BORROWINGS = 0  # Microsoft reports none

EXPECTED_2026 = {
    "current_ratio": CURRENT_ASSETS / CURRENT_LIABILITIES,
    "quick_ratio": (CASH + SHORT_TERM_INVESTMENTS + RECEIVABLES) / CURRENT_LIABILITIES,
    "cash_flow_ratio": OPERATING_CASH_FLOW / CURRENT_LIABILITIES,
    "debt_to_equity": (SHORT_TERM_BORROWINGS + CURRENT_LONG_TERM_DEBT + LONG_TERM_DEBT) / EQUITY,
    "gross_margin": GROSS_PROFIT / REVENUE,
    "net_margin": NET_INCOME / REVENUE,
    "asset_turnover": REVENUE / TOTAL_ASSETS,
    "equity_multiplier": TOTAL_ASSETS / EQUITY,
    "roe": NET_INCOME / EQUITY,
}


@pytest.fixture(scope="module")
def results():
    return {(r.metric_id, r.fiscal_year): r for r in compute_all(load_facts(FIXTURE))}


@pytest.mark.parametrize("metric_id", sorted(EXPECTED_2026))
def test_matches_the_filing(results, metric_id):
    assert results[(metric_id, 2026)].value == pytest.approx(EXPECTED_2026[metric_id], rel=1e-9)


def test_dupont_identity_holds(results):
    """ROE = net margin x asset turnover x equity multiplier (B&D eq. 2.23)."""
    for year in (2026, 2025):
        parts = ["net_margin", "asset_turnover", "equity_multiplier"]
        product = 1.0
        for part in parts:
            product *= results[(part, year)].value
        assert product == pytest.approx(results[("roe", year)].value, rel=1e-12)


def test_every_metric_records_the_numbers_it_used(results):
    result = results[("quick_ratio", 2026)]
    assert set(result.inputs) == set(METRICS_BY_ID["quick_ratio"].inputs)
    assert result.inputs["cash"] == CASH * 1e6


def test_years_the_filing_cannot_support_are_skipped(results):
    """The balance sheet has 2 dates, so no current ratio for the third year."""
    assert ("current_ratio", 2024) not in results
    # But ROE only needs net income and equity, and equity has a third year.
    assert results[("roe", 2024)].value is not None


def test_descriptions_cover_every_metric():
    text = describe_metrics()
    assert all(m.id in text for m in METRICS)


# --- the cases where there is no answer ----------------------------------


def facts(**line_items) -> list[Fact]:
    """Facts for one year, e.g. facts(equity=-5, net_income=10)."""
    return [
        Fact(
            line_item=name,
            fiscal_year=2026,
            value=float(value),
            concept="us-gaap:Test",
            period="2026-06-30",
            accession="acc",
        )
        for name, value in line_items.items()
    ]


def only(results, metric_id):
    return [r for r in results if r.metric_id == metric_id][0]


def test_missing_input_gives_no_value_and_says_which():
    result = only(compute_all(facts(revenue=100), ["net_margin"]), "net_margin")
    assert result.value is None
    assert "net_income" in result.reason


def test_zero_denominator_gives_no_value():
    result = only(compute_all(facts(revenue=0, net_income=10), ["net_margin"]), "net_margin")
    assert result.value is None
    assert "zero" in result.reason


def test_negative_equity_gives_no_value():
    """Buybacks can push equity below zero, and then ROE misleads rather than informs."""
    results = compute_all(facts(equity=-5_000, net_income=1_000, total_assets=10_000), ["roe"])
    assert only(results, "roe").value is None
    assert "negative" in only(results, "roe").reason


def test_a_company_whose_receivables_tag_we_miss_gets_no_quick_ratio():
    """Costco tags receivables in a way this tool does not recognise. Treating
    that as zero would understate its liquidity while looking like an answer."""
    unreported = [
        Fact(
            line_item=name,
            fiscal_year=2026,
            value=0.0,
            concept="",
            period="2026-06-30",
            accession="acc",
            reported=False,
        )
        for name in ("short_term_investments", "accounts_receivable")
    ]
    results = compute_all(facts(cash=100, current_liabilities=500) + unreported, ["quick_ratio"])
    assert only(results, "quick_ratio").value is None
    assert "receivable" in only(results, "quick_ratio").reason


def test_a_company_with_no_debt_lines_is_not_treated_as_debt_free():
    unreported = [
        Fact(
            line_item=name,
            fiscal_year=2026,
            value=0.0,
            concept="",
            period="2026-06-30",
            accession="acc",
            reported=False,
        )
        for name in ("short_term_borrowings", "current_long_term_debt", "long_term_debt")
    ]
    results = compute_all(facts(equity=1_000) + unreported, ["debt_to_equity"])
    assert only(results, "debt_to_equity").value is None
    assert "no short_term_borrowings or current_long_term_debt" in only(results, "debt_to_equity").reason


def test_zero_debt_lines_that_are_reported_do_compute():
    reported_zero = [
        Fact(
            line_item=name,
            fiscal_year=2026,
            value=0.0,
            concept="us-gaap:Test",
            period="2026-06-30",
            accession="acc",
        )
        for name in ("short_term_borrowings", "current_long_term_debt", "long_term_debt")
    ]
    results = compute_all(facts(equity=1_000) + reported_zero, ["debt_to_equity"])
    assert only(results, "debt_to_equity").value == 0.0
