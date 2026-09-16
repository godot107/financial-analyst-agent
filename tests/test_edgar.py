"""Step 1: the facts we pull out of a 10-K.

The fixture tests run against a recorded filing, so they need no network.
The rest check the selection rules with small hand-made tables.
"""

from pathlib import Path

import pandas as pd
import pytest

from fin_analyst.edgar import Fact, fetch_facts, load_facts, select_facts

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
