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
OPERATING_INCOME = 155_237
INTEREST_EXPENSE = 3_051  # "Other income (expense), net" note
OPERATING_LEASES = 21_925  # lease note: current part sits in other current liabilities
FINANCE_LEASES = 66_594  # lease note: in other current and long-term liabilities, not debt
EQUITY_2025 = 343_479
DEBT = SHORT_TERM_BORROWINGS + CURRENT_LONG_TERM_DEBT + LONG_TERM_DEBT

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
    "operating_margin": OPERATING_INCOME / REVENUE,
    "interest_coverage": OPERATING_INCOME / INTEREST_EXPENSE,
    "debt_to_capital": DEBT / (DEBT + EQUITY),
    "debt_to_equity_with_leases": (DEBT + OPERATING_LEASES + FINANCE_LEASES) / EQUITY,
    "roe_average_equity": NET_INCOME / ((EQUITY + EQUITY_2025) / 2),
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


# --- solvency, leases, averages, and companies these ratios don't fit ------


def test_an_averaged_ratio_records_both_balances(results):
    result = results[("roe_average_equity", 2026)]
    assert result.inputs["equity_prior_year"] == EQUITY_2025 * 1e6
    # 2025 averages with 2024's equity, which the equity statement reports; 2024
    # is the earliest equity figure, with no year before it to average with.
    assert results[("roe_average_equity", 2025)].inputs["equity_prior_year"] > 0
    assert ("roe_average_equity", 2024) not in results


def test_a_line_that_stops_being_reported_is_unavailable_for_the_latest_year():
    """Apple dropped interest expense after 2023. Coverage must not end quietly in 2023."""
    history = facts(revenue=100, operating_income=30, interest_expense=1)
    history = [f.model_copy(update={"fiscal_year": 2023}) for f in history]
    history += facts(revenue=120, operating_income=40)  # 2026: no interest expense
    results = compute_all(history, ["interest_coverage"])

    assert [r.fiscal_year for r in results] == [2026, 2023]
    assert results[0].value is None and "interest_expense" in results[0].reason


def test_leases_count_only_when_some_lease_line_is_reported():
    unreported = [
        Fact(line_item=name, fiscal_year=2026, value=0.0, concept="", period="2026-06-30",
             accession="acc", reported=False)
        for name in ("operating_lease_liabilities", "finance_lease_liabilities")
    ]
    results = compute_all(
        facts(equity=1_000, short_term_borrowings=0, current_long_term_debt=0, long_term_debt=100)
        + unreported,
        ["debt_to_equity_with_leases"],
    )
    assert only(results, "debt_to_equity_with_leases").value is None
    assert "lease" in only(results, "debt_to_equity_with_leases").reason


def bank(**more):
    """A balance sheet with no current/non-current split, like JPMorgan's."""
    return facts(total_assets=4_000, equity=300, net_income=50, revenue=180,
                 long_term_debt=40, **more)


@pytest.mark.parametrize(
    "metric_id", ["current_ratio", "quick_ratio", "debt_to_equity", "interest_coverage", "debt_to_capital"]
)
def test_ratios_that_do_not_fit_a_bank_say_so(metric_id):
    """JPMorgan came out at 0.18x debt to equity: its debt isn't split into the
    lines this tool reads, so the number was a sliver of the real one."""
    result = only(compute_all(bank(interest_expense=10), [metric_id]), metric_id)
    assert result.value is None
    assert "banks and insurers" in result.reason


def test_a_bank_still_gets_the_ratios_that_do_fit():
    results = compute_all(bank(), ["roe", "equity_multiplier"])
    assert only(results, "roe").value == pytest.approx(50 / 300)
    assert only(results, "equity_multiplier").value == pytest.approx(4_000 / 300)


# --- the ratios banks and insurers do have -------------------------------

# JPMorgan's FY2025 10-K, $ millions.
JPM = dict(total_assets=4_424_900, equity=362_438, net_income=57_048,
           net_interest_income=95_443, noninterest_income=87_004, noninterest_expense=95_640,
           credit_loss_provision=14_212, loans=1_467_664, deposits=2_559_320)
# Travelers' FY2025 10-K, $ millions.
TRV = dict(total_assets=143_708, equity=32_894, net_income=6_288, premiums_earned=43_914,
           claims_incurred=27_221, policy_acquisition_costs=7_266, selling_general_admin=6_120)


def test_a_bank_gets_the_ratios_its_statements_support():
    results = compute_all(facts(**JPM), ["efficiency_ratio", "loans_to_deposits", "credit_cost_to_loans"])
    assert only(results, "efficiency_ratio").value == pytest.approx(95_640 / (95_443 + 87_004))
    assert only(results, "loans_to_deposits").value == pytest.approx(1_467_664 / 2_559_320)
    assert only(results, "credit_cost_to_loans").value == pytest.approx(14_212 / 1_467_664)


def test_the_interest_margin_proxy_averages_the_balance_sheet_and_says_it_is_a_proxy():
    last_year = [f.model_copy(update={"fiscal_year": 2025}) for f in facts(total_assets=4_002_814)]
    results = compute_all(facts(**JPM) + last_year, ["net_interest_to_assets"])
    result = only(results, "net_interest_to_assets")

    assert result.value == pytest.approx(95_443 / ((4_424_900 + 4_002_814) / 2))
    assert "earning assets" in METRICS_BY_ID["net_interest_to_assets"].description


def test_an_insurer_gets_its_underwriting_ratios():
    results = compute_all(facts(**TRV), ["claims_to_premiums", "underwriting_cost_to_premiums"])
    assert only(results, "claims_to_premiums").value == pytest.approx(27_221 / 43_914)
    assert only(results, "underwriting_cost_to_premiums").value == pytest.approx((7_266 + 6_120) / 43_914)


@pytest.mark.parametrize(
    "metric_id, company",
    [("efficiency_ratio", TRV), ("claims_to_premiums", JPM), ("loans_to_deposits", TRV)],
)
def test_a_ratio_for_the_other_kind_of_company_is_unavailable(metric_id, company):
    result = only(compute_all(facts(revenue=100, **company), [metric_id]), metric_id)
    assert result.value is None and "the filing has no" in result.reason
