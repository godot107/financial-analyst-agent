"""The two helpers for repetitive work: a batch of memos, and a tag report."""

from pathlib import Path

import pytest

from fin_analyst.batch import run_batch, summarize
from fin_analyst.config import load_settings
from fin_analyst.coverage import concerns, line_item_coverage, metric_coverage
from fin_analyst.edgar import Fact, load_facts
from tests.test_graph import CLEAN_DRAFT, FIXTURE, LEAKY_DRAFT, FakeAnalyst

FACTS = load_facts(FIXTURE)


@pytest.fixture
def settings():
    return load_settings()


def batch(tmp_path, tickers, drafts, cost=0.01, **kwargs):
    analyst = FakeAnalyst(drafts, cost=cost)
    return run_batch(
        tickers, "How liquid is it?", analyst, load_settings(), tmp_path,
        fetch=lambda ticker: FACTS, fetch_text=None, **kwargs
    )


# --- the batch ------------------------------------------------------------


def test_each_ticker_gets_a_memo_and_the_summary_lists_them(tmp_path):
    items = batch(tmp_path, ["msft", "cost"], [CLEAN_DRAFT] * 2)

    assert [i.ticker for i in items] == ["MSFT", "COST"]
    assert all(i.memo_file and i.error is None for i in items)
    assert len(list(tmp_path.glob("*.md"))) == 3  # two memos plus the summary

    summary = (tmp_path / "summary.md").read_text()
    assert "2/2 answered" in summary
    assert "MSFT" in summary and "COST" in summary


def test_one_bad_ticker_does_not_end_the_batch(tmp_path):
    def fetch(ticker):
        if ticker == "NOPE":
            raise ValueError("no facts found in the latest 10-K for NOPE")
        return FACTS

    analyst = FakeAnalyst([CLEAN_DRAFT] * 2)
    items = run_batch(
        ["nope", "msft"], "How liquid is it?", analyst, load_settings(), tmp_path,
        fetch=fetch, fetch_text=None,
    )

    assert items[0].error and "no facts found" in items[0].error
    assert items[1].memo_file, "the batch stopped at the first failure"


def test_a_company_that_gives_up_is_recorded_without_a_memo(tmp_path):
    items = batch(tmp_path, ["msft"], [LEAKY_DRAFT] * 3)

    assert items[0].memo_file is None
    assert "gave up" in items[0].error
    assert items[0].drafts == 3  # the rejected drafts are still in the record


def test_the_batch_stops_at_its_total_cap(tmp_path):
    """The per-memo guard would let ten companies spend ten times over."""
    items = batch(tmp_path, ["a", "b", "c"], [CLEAN_DRAFT] * 3, cost=0.05, max_usd_total=0.12)

    assert items[0].memo_file and items[1].memo_file  # 2 x $0.10 = $0.20 > cap
    assert "skipped" in items[2].error
    assert "$0.12" in items[2].error


def test_the_summary_reads_as_a_table():
    from fin_analyst.batch import BatchItem

    text = summarize(
        [
            BatchItem(ticker="MSFT", memo_file="MSFT-x.md", cost_usd=0.03, drafts=1),
            BatchItem(ticker="JPM", error="gave up after 3 drafts", drafts=3),
        ],
        "How liquid is it?",
    )
    assert "| MSFT | MSFT-x.md | 1 | $0.0300 |" in text
    assert "gave up after 3 drafts" in text
    assert "1/2 answered" in text


# --- the coverage report --------------------------------------------------


def test_coverage_names_the_tag_each_line_item_came_from():
    rows = {row.line_item: row for row in line_item_coverage(FACTS)}
    assert rows["total_assets"].concept == "us-gaap:Assets"
    assert rows["total_assets"].status == "found"
    assert rows["total_assets"].years == [2026, 2025]


def test_a_zero_filled_line_is_flagged_for_a_human():
    """Microsoft reports no commercial paper; a tag we failed to recognise would
    look exactly the same, so it gets flagged either way."""
    rows = {row.line_item: row for row in line_item_coverage(FACTS)}
    assert rows["short_term_borrowings"].status == "treated as zero"
    assert any("short_term_borrowings" in note for note in concerns(FACTS))


def test_a_missing_line_item_explains_which_metrics_it_costs():
    without_gross_profit = [f for f in FACTS if f.line_item != "gross_profit"]
    notes = concerns(without_gross_profit)

    assert any("gross_profit: not found at all" in note for note in notes)
    assert any("gross_margin: no year could be computed" in note for note in notes)


def test_metric_coverage_separates_computed_years_from_reasons():
    rows = {row.metric_id: row for row in metric_coverage(FACTS)}
    assert rows["roe"].years == [2026, 2025, 2024]
    # The balance sheet has two dates, so the third year has no equity multiplier -
    # and no row either: 2024's equity comes from the equity statement, not a balance sheet.
    assert rows["equity_multiplier"].years == [2026, 2025]
    assert 2024 not in rows["equity_multiplier"].unavailable
    # A line missing from a year that has a balance sheet does get a reason.
    without_debt = [f for f in FACTS if not (f.line_item == "long_term_debt" and f.fiscal_year == 2025)]
    assert 2025 in {row.metric_id: row for row in metric_coverage(without_debt)}["debt_to_equity"].unavailable


def test_a_filing_with_nothing_in_it_flags_everything():
    empty: list[Fact] = []
    assert len(concerns(empty)) > 10
