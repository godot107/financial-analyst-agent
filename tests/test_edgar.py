"""Step 1: the facts we pull out of a 10-K.

The fixture tests run against a recorded filing, so they need no network.
The rest check the selection rules with small hand-made tables.
"""

from pathlib import Path

import pandas as pd
import pytest

from fin_analyst.edgar import (
    Fact, describe_filing, fetch_facts, load_facts, merge_filings, select_facts,
)

FIXTURE = Path(__file__).parent / "fixtures" / "msft_facts.json"


@pytest.fixture(scope="module")
def msft():
    return load_facts(FIXTURE)


def by_item(facts, line_item):
    return {f.fiscal_year: f for f in facts if f.line_item == line_item}


# --- the recorded filing -------------------------------------------------


def test_balance_sheet_balances(msft):
    """Assets = liabilities + equity. Catches wrong periods, wrong scale, wrong tag."""
    assets = by_item(msft, "total_assets")
    both_sides = by_item(msft, "liabilities_and_equity")
    assert assets, "no total assets in the fixture"

    for year, asset in assets.items():
        assert year in both_sides
        assert asset.value == pytest.approx(both_sides[year].value, rel=0.005)


def test_current_assets_are_part_of_total_assets(msft):
    assets = by_item(msft, "total_assets")
    for year, current in by_item(msft, "current_assets").items():
        assert 0 < current.value <= assets[year].value


def test_every_fact_says_where_it_came_from(msft):
    for fact in msft:
        assert fact.accession, f"{fact.line_item} {fact.fiscal_year} has no accession number"
        assert fact.period
        if fact.reported:
            assert fact.concept.startswith("us-gaap:")
        else:
            # Not on the statement: recorded as 0, with no tag to point at.
            assert fact.value == 0 and fact.concept == ""


def test_income_statement_covers_more_years_than_the_balance_sheet(msft):
    # A 10-K shows 3 years of income but only 2 balance sheet dates.
    assert len(by_item(msft, "revenue")) >= 3
    assert len(by_item(msft, "total_assets")) == 2


# --- the selection rules -------------------------------------------------


def make_rows(rows):
    """Build the columns select_facts reads, filling in sensible defaults."""
    defaults = {
        "is_dimensioned": False,
        "numeric_value": 1.0,
        "period_type": "instant",
        "fiscal_period": None,
        "period_instant": "2026-06-30",
        "period_end": "2026-06-30",
        "fiscal_year": 2026,
    }
    return pd.DataFrame([{**defaults, **row} for row in rows])


def test_segment_and_component_rows_are_ignored():
    """Dimensioned rows break the same concept down by segment or equity component."""
    df = make_rows(
        [
            {"concept": "us-gaap:Assets", "numeric_value": 999.0, "is_dimensioned": True},
            {"concept": "us-gaap:Assets", "numeric_value": 100.0, "is_dimensioned": False},
        ]
    )
    facts = select_facts(df, "0000-00-000000")
    assets = by_item(facts, "total_assets")
    assert [f.value for f in assets.values()] == [100.0]


def test_first_matching_tag_wins_and_is_recorded():
    """Companies tag revenue differently; we record which tag we used."""
    df = make_rows(
        [
            {
                "concept": "us-gaap:Revenues",
                "numeric_value": 50.0,
                "period_type": "duration",
                "fiscal_period": "FY",
            }
        ]
    )
    revenue = by_item(select_facts(df, "acc"), "revenue")[2026]
    assert revenue.value == 50.0
    assert revenue.concept == "us-gaap:Revenues"


def test_quarterly_figures_are_left_out():
    df = make_rows(
        [
            {
                "concept": "us-gaap:Revenues",
                "numeric_value": 10.0,
                "period_type": "duration",
                "fiscal_period": "Q4",
            }
        ]
    )
    assert by_item(select_facts(df, "acc"), "revenue") == {}


def test_unreported_optional_lines_become_zero():
    """A company with no commercial paper has no commercial paper line: that is 0."""
    df = make_rows([{"concept": "us-gaap:Assets", "numeric_value": 100.0}])
    borrowings = by_item(select_facts(df, "acc"), "short_term_borrowings")[2026]
    assert borrowings.value == 0.0
    assert borrowings.reported is False


def test_missing_required_lines_are_simply_absent():
    """Gross profit isn't optional: if it's missing the metric must fail, not read 0."""
    df = make_rows([{"concept": "us-gaap:Assets", "numeric_value": 100.0}])
    facts = select_facts(df, "acc")
    assert by_item(facts, "gross_profit") == {}


def test_fetch_needs_an_sec_identity(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        fetch_facts("MSFT")


def test_balance_sheet_dates_are_dated_by_the_income_statement():
    """A late-August fiscal year puts the prior year-end in the next calendar
    year, and the filing labels both with the same fiscal year. Trusting that
    would let last year's balance sheet overwrite this year's."""
    df = make_rows(
        [
            # Two years of income, which date the two balance sheets.
            {
                "concept": "us-gaap:Revenues",
                "numeric_value": 200.0,
                "period_type": "duration",
                "fiscal_period": "FY",
                "period_end": "2025-08-31",
                "fiscal_year": 2025,
            },
            {
                "concept": "us-gaap:Revenues",
                "numeric_value": 180.0,
                "period_type": "duration",
                "fiscal_period": "FY",
                "period_end": "2024-09-01",
                "fiscal_year": 2024,
            },
            # Both balance sheet dates arrive labelled 2025, as edgartools does.
            {"concept": "us-gaap:Assets", "numeric_value": 100.0, "period_instant": "2025-08-31", "fiscal_year": 2025},
            {"concept": "us-gaap:Assets", "numeric_value": 90.0, "period_instant": "2024-09-01", "fiscal_year": 2025},
        ]
    )
    assets = by_item(select_facts(df, "acc"), "total_assets")
    assert assets[2025].value == 100.0
    assert assets[2024].value == 90.0


def test_the_fixture_has_one_value_per_line_item_and_year(msft):
    seen = [(f.line_item, f.fiscal_year) for f in msft]
    assert len(seen) == len(set(seen)), "a duplicate means two dates collided on one year"


def test_the_fiscal_year_comes_from_the_date_not_the_filing_s_label():
    """Costco's FY2024 figures arrive labelled 2025 by edgartools."""
    df = make_rows(
        [
            {
                "concept": "us-gaap:Revenues",
                "numeric_value": 254.0,
                "period_type": "duration",
                "fiscal_period": "FY",
                "period_end": "2024-09-01",
                "fiscal_year": 2025,  # what the filing says, and it is wrong
            }
        ]
    )
    assert by_item(select_facts(df, "acc"), "revenue")[2024].value == 254.0


def test_a_filing_is_described_from_its_index_entry_alone():
    """No download: only what the latest-10-K lookup already returned."""
    from types import SimpleNamespace

    class IndexEntry:
        cik, company, form = 789019, "MICROSOFT CORP", "10-K"
        filing_date, accession_no = "2026-07-30", "0001193125-26-323660"

        @property
        def period_of_report(self):
            raise AssertionError("this property downloads the whole submission")

    company = lambda ticker: SimpleNamespace(
        get_filings=lambda form: SimpleNamespace(latest=lambda: IndexEntry())
    )
    filing = describe_filing("msft", identity="Test test@example.com", company=company)

    assert filing.ticker == "MSFT" and filing.company == "MICROSOFT CORP" and filing.cik == 789019
    assert filing.filed == "2026-07-30" and filing.period is None
    assert filing.url == "https://www.sec.gov/Archives/edgar/data/789019/0001193125-26-323660-index.html"


# --- leases ------------------------------------------------------------------


def test_a_lease_total_is_used_when_the_company_reports_one():
    """Microsoft reports only the total; its current part hides in other liabilities."""
    df = make_rows([
        {"concept": "us-gaap:Assets", "numeric_value": 100.0},
        {"concept": "us-gaap:OperatingLeaseLiability", "numeric_value": 21_925.0},
        {"concept": "us-gaap:OperatingLeaseLiabilityNoncurrent", "numeric_value": 16_532.0},
    ])
    leases = by_item(select_facts(df, "acc"), "operating_lease_liabilities")[2026]
    assert leases.value == 21_925.0 and leases.concept == "us-gaap:OperatingLeaseLiability"


def test_lease_parts_are_added_when_there_is_no_total():
    df = make_rows([
        {"concept": "us-gaap:Assets", "numeric_value": 100.0},
        {"concept": "us-gaap:FinanceLeaseLiabilityCurrent", "numeric_value": 100.0},
        {"concept": "us-gaap:FinanceLeaseLiabilityNoncurrent", "numeric_value": 900.0},
    ])
    leases = by_item(select_facts(df, "acc"), "finance_lease_liabilities")[2026]
    assert leases.value == 1_000.0 and "+" in leases.concept


def test_half_a_lease_liability_is_not_a_lease_liability():
    """Only the current part found: better unreported (and no ratio) than half the figure."""
    df = make_rows([
        {"concept": "us-gaap:Assets", "numeric_value": 100.0},
        {"concept": "us-gaap:OperatingLeaseLiabilityCurrent", "numeric_value": 100.0},
    ])
    leases = by_item(select_facts(df, "acc"), "operating_lease_liabilities")[2026]
    assert leases.reported is False


def test_finance_leases_already_inside_debt_are_not_counted_twice():
    df = make_rows([
        {"concept": "us-gaap:Assets", "numeric_value": 100.0},
        {"concept": "us-gaap:LongTermDebtAndCapitalLeaseObligationsNoncurrent", "numeric_value": 500.0},
        {"concept": "us-gaap:FinanceLeaseLiability", "numeric_value": 80.0},
    ])
    leases = by_item(select_facts(df, "acc"), "finance_lease_liabilities")[2026]
    assert leases.value == 0.0 and leases.reported
    assert leases.concept == "included in us-gaap:LongTermDebtAndCapitalLeaseObligationsNoncurrent"


# --- several filings ---------------------------------------------------------


def fact(item, year, value, accession, reported=True):
    return Fact(line_item=item, fiscal_year=year, value=value, concept="us-gaap:X" if reported else "",
                period=f"{year}-06-30", accession=accession, reported=reported)


def test_the_later_filing_wins_and_the_first_filed_figure_is_kept():
    newest = [fact("revenue", 2026, 120, "new"), fact("revenue", 2025, 105, "new")]
    older = [fact("revenue", 2025, 100, "old"), fact("revenue", 2024, 90, "old")]
    merged = {f.fiscal_year: f for f in merge_filings([newest, older])}

    assert sorted(merged) == [2024, 2025, 2026]
    assert merged[2025].value == 105 and merged[2025].accession == "new"
    assert merged[2025].earlier_value == 100 and merged[2025].earlier_accession == "old"
    assert merged[2024].earlier_value is None


def test_a_reported_figure_beats_a_zero_filled_one_from_a_later_filing():
    newest = [fact("long_term_debt", 2025, 0.0, "new", reported=False)]
    older = [fact("long_term_debt", 2025, 700, "old")]
    [merged] = merge_filings([newest, older])
    assert merged.value == 700 and merged.reported and merged.earlier_value is None


def test_several_filings_are_read_newest_first():
    from types import SimpleNamespace

    class Entry:
        def __init__(self, accession, assets):
            self.accession_no, self.assets = accession, assets

        def xbrl(self):
            rows = make_rows([{"concept": "us-gaap:Assets", "numeric_value": self.assets,
                               "period_instant": f"{self.assets:.0f}-06-30"}])
            return SimpleNamespace(facts=SimpleNamespace(to_dataframe=lambda: rows))

    entries = [Entry("new", 2026.0), Entry("old", 2025.0)]
    company = lambda ticker: SimpleNamespace(get_filings=lambda form: SimpleNamespace(
        latest=lambda n=1: entries[:n] if n > 1 else entries[0]))
    facts = fetch_facts("MSFT", identity="Test test@example.com", company=company, filings=2)

    assert sorted(f.fiscal_year for f in facts if f.line_item == "total_assets") == [2025, 2026]
    with pytest.raises(ValueError, match="between 1 and 5"):
        fetch_facts("MSFT", identity="Test test@example.com", company=company, filings=9)
