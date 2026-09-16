"""Step 3: the check that keeps Claude's numbers out, and the renderer that puts ours in."""

import pytest

from fin_analyst.edgar import Fact
from fin_analyst.memo import build_footer, find_problems, render
from fin_analyst.metrics import MetricResult


def result(metric_id="current_ratio", year=2026, value=1.23, unit="ratio", reason=None):
    return MetricResult(
        metric_id=metric_id, fiscal_year=year, value=value, unit=unit, reason=reason
    )


METRICS = [
    result(year=2026, value=1.2303),
    result(year=2025, value=1.3534),
    result("roe", 2026, 0.302336, "percent"),
    result("roe", 2025, 0.296472, "percent"),
    result("debt_to_equity", 2026, None, "ratio", reason="equity is negative for 2026"),
]


# --- what counts as a problem -------------------------------------------


def test_a_clean_draft_has_no_problems():
    draft = "Liquidity slipped: the current ratio {{current_ratio:2025->2026}}, while ROE was {{roe:2026}}."
    assert find_problems(draft, METRICS) == []


@pytest.mark.parametrize(
    "draft",
    [
        "The current ratio of 1.23 covers it.",  # a plain number
        "Margins reached 40.3% this year.",  # a percentage
        "Assets are roughly 2x equity.",  # a multiple
        "A ratio above 1 is usually healthy.",  # a rule of thumb
        "Revenue grew by a third to $331 billion.",  # a number in words plus digits
    ],
)
def test_numbers_the_model_typed_are_caught(draft):
    problems = find_problems(draft, METRICS)
    assert problems and "wrote yourself" in problems[0]


@pytest.mark.parametrize(
    "draft",
    [
        "Fiscal 2026 was stronger than fiscal 2025.",
        "FY2026 was stronger than FY2025.",  # no word boundary between Y and 2
        "FY 2026 was stronger.",
    ],
)
def test_fiscal_years_in_the_data_are_allowed(draft):
    assert find_problems(draft, METRICS) == []


def test_a_fiscal_year_not_in_the_data_is_still_caught():
    assert find_problems("FY2019 was different.", METRICS)


def test_a_year_not_in_the_data_is_still_a_leak():
    """Otherwise the model could smuggle in figures it remembers from other years."""
    assert find_problems("Back in 2019 the picture was different.", METRICS)


def test_form_names_are_allowed():
    assert find_problems("Figures come from the 10-K.", METRICS) == []


def test_placeholders_that_do_not_exist_are_caught():
    problems = find_problems("Inventory turns were {{inventory_turnover:2026}}.", METRICS)
    assert "not a placeholder you were given" in problems[0]

    problems = find_problems("ROE in {{roe:2019}} was strong.", METRICS)
    assert "no roe for 2019" in problems[0]


def test_every_problem_is_reported_not_just_the_first():
    draft = "The ratio was 1.23 and {{made_up:2026}} too, with 5% growth."
    assert len(find_problems(draft, METRICS)) == 3


# --- rendering ------------------------------------------------------------


def test_values_are_formatted_by_unit():
    assert render("{{current_ratio:2026}}", METRICS) == "1.23x"
    assert render("{{roe:2026}}", METRICS) == "30.2%"


def test_changes_render_their_own_direction():
    """The model never writes 'rose' or 'fell', so it cannot get the direction wrong."""
    assert render("{{current_ratio:2025->2026}}", METRICS) == "fell 0.12x to 1.23x"
    assert render("{{roe:2025->2026}}", METRICS) == "rose 0.6 pts to 30.2%"


def test_a_verb_typed_in_front_of_a_change_placeholder_is_dropped():
    """Otherwise the rendered memo reads "fell fell 0.12x to 1.23x"."""
    assert render("The ratio fell {{current_ratio:2025->2026}}.", METRICS) == (
        "The ratio fell 0.12x to 1.23x."
    )
    assert render("It ROSE {{roe:2025->2026}}.", METRICS) == "It rose 0.6 pts to 30.2%."
    # A verb not attached to a change placeholder is the writer's own prose.
    assert render("Liquidity fell overall.", METRICS) == "Liquidity fell overall."


def test_an_unchanged_metric_says_so():
    flat = [result(year=2025, value=1.5), result(year=2026, value=1.5)]
    assert render("{{current_ratio:2025->2026}}", flat) == "was unchanged at 1.50x"


def test_a_metric_with_no_value_renders_its_reason():
    assert "not available (equity is negative for 2026)" == render("{{debt_to_equity:2026}}", METRICS)


def test_a_change_with_a_missing_side_renders_the_reason():
    metrics = [result("roe", 2025, None, "percent", reason="equity is negative for 2025"), result("roe", 2026, 0.3, "percent")]
    assert "not available" in render("{{roe:2025->2026}}", metrics)


# --- the footer -----------------------------------------------------------


def test_footer_says_where_the_numbers_came_from_and_what_to_distrust():
    facts = [
        Fact(
            line_item="total_assets",
            fiscal_year=2026,
            value=10.0,
            concept="us-gaap:Assets",
            period="2026-06-30",
            accession="0001193125-26-323660",
        ),
        Fact(
            line_item="short_term_borrowings",
            fiscal_year=2026,
            value=0.0,
            concept="",
            period="2026-06-30",
            accession="0001193125-26-323660",
            reported=False,
        ),
    ]
    footer = build_footer(facts, METRICS)
    assert "0001193125-26-323660" in footer
    assert "2026-06-30" in footer
    assert "ending balances" in footer
    assert "No peer comparison" in footer
    assert "short_term_borrowings" in footer


def test_rendered_memo_keeps_the_footer_separate():
    memo = render("ROE was {{roe:2026}}.", METRICS, footer="---\nSource: test")
    assert memo.startswith("ROE was 30.2%.")
    assert memo.endswith("---\nSource: test")
