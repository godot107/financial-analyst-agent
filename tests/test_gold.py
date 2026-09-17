"""The gold set stays consistent with the code, without touching the network.

evals/gold.py grades against SEC data and Claude; this only checks the file
itself, so a renamed metric or line item can't quietly make a check vacuous.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

from gold import grade_question, load_gold  # noqa: E402

from fin_analyst.edgar import BALANCE_SHEET_ITEMS, INCOME_AND_CASH_FLOW_ITEMS, LEASE_ITEMS  # noqa: E402
from fin_analyst.metrics import METRICS_BY_ID, MetricResult  # noqa: E402

GOLD = load_gold()
LINE_ITEMS = {**BALANCE_SHEET_ITEMS, **INCOME_AND_CASH_FLOW_ITEMS, **LEASE_ITEMS}


def test_every_gold_figure_is_a_line_item_with_evidence():
    for ticker, company in GOLD["companies"].items():
        for item, gold in company["facts"].items():
            assert item in LINE_ITEMS, f"{ticker}: {item} is not a line item"
            assert gold["evidence"] and gold["value"] > 0


def test_every_ratio_checked_has_all_its_inputs_in_the_gold_figures():
    """Otherwise the 'expected' value would lean on a zero nobody checked."""
    optional_zero = {"short_term_borrowings", "current_long_term_debt", "short_term_investments",
                     "accounts_receivable", "finance_lease_liabilities"}
    for metric_id, tickers in GOLD["ratios"].items():
        metric = METRICS_BY_ID[metric_id]
        for ticker in tickers:
            have = set(GOLD["companies"][ticker]["facts"])
            missing = set(metric.inputs) - have - optional_zero
            assert not missing, f"{ticker} {metric_id} needs gold figures for {missing}"


def test_questions_name_real_companies_and_metrics():
    for case in GOLD["questions"]:
        assert case["ticker"] in GOLD["companies"]
        for group in case.get("choose_any", []):
            assert set(group) <= set(METRICS_BY_ID), group
    for company in GOLD["companies"].values():
        assert set(company.get("unavailable") or {}) <= set(METRICS_BY_ID)


def test_a_question_is_graded_on_plan_memo_and_wording():
    case = {"choose_any": [["current_ratio"]], "memo_excludes": ["Microsoft 365"],
            "memo_includes_any": ["liquid"], "min_years": 2}
    state = SimpleNamespace(
        memo="Microsoft is liquid; Microsoft 365 revenue grew.", error=None, drafts=["d1", "d2"],
        metric_ids=["quick_ratio"],
        metrics=[MetricResult(metric_id="quick_ratio", fiscal_year=2026, value=1.0, unit="ratio")],
    )
    grades = {what: ok for what, ok, _ in grade_question(case, state)}
    assert grades == {
        "memo published": True,
        "chose one of ['current_ratio']": False,
        "memo says one of ['liquid']": True,
        "memo avoids 'Microsoft 365'": False,
        "at least 2 years": False,
        "first draft passed": False,
    }
