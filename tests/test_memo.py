"""Step 3: the check that keeps Claude's numbers out, and the renderer that puts ours in."""

import pytest

from fin_analyst.edgar import Fact
from fin_analyst.memo import build_footer, cited_claims, find_problems, render
from fin_analyst.metrics import MetricResult
from fin_analyst.passages import Passage


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
    assert any("wrote yourself" in p for p in problems)


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


# --- names that contain digits -------------------------------------------


def filing_passage(text):
    return Passage(id="P1", item="Item 7", text=text, ticker="MSFT", accession="acc")


def test_a_product_name_with_digits_is_not_a_leak():
    """"Microsoft 365" is a name the filing uses, and blocking it costs a retry."""
    passages = [filing_passage("Gross margin grew in Microsoft 365 Commercial cloud.")]
    assert find_problems("Growth came from Microsoft 365 [P1].", METRICS, (), passages) == []


@pytest.mark.parametrize("leak", ["$13.8 billion", "21%", "1.23", "66%"])
def test_measurements_are_still_caught_even_when_the_passage_contains_them(leak):
    """The rule admits names, never figures - a measurement carries a decimal,
    a currency symbol or a percent sign."""
    passages = [filing_passage(f"Gross margin increased {leak} driven by Azure.")]
    problems = find_problems(f"Gross margin increased {leak} [P1].", METRICS, (), passages)
    assert any("wrote yourself" in p for p in problems)


def test_a_number_not_in_the_filing_is_still_a_leak():
    passages = [filing_passage("Gross margin grew in Microsoft 365 Commercial cloud.")]
    assert find_problems("There are 500 data centres.", METRICS, (), passages)


# --- what the claim check reads ------------------------------------------


def test_only_sentences_that_cite_the_filing_are_checked():
    passages = [filing_passage("Azure grew."), Passage(id="P2", item="Item 1A", text="Risk.", ticker="MSFT", accession="acc")]
    draft = (
        "Margins fell {{net_margin:2025->2026}}. "
        "The filing attributes this to Azure investment [P1]. "
        "Liquidity is unrelated. "
        "Two passages agree [P1][P2]."
    )
    claims = cited_claims(draft, passages)

    assert len(claims) == 2  # the uncited sentences are not the judge's business
    assert claims[0][0].endswith("[P1].")
    assert [p.id for p in claims[1][1]] == ["P1", "P2"]


def test_a_citation_nobody_gave_us_is_not_sent_to_the_judge():
    """The checker already rejects it; the judge should never see a dangling id."""
    assert cited_claims("Claimed [P99].", [filing_passage("text")]) == []


def test_a_draft_with_no_citations_needs_no_checking():
    assert cited_claims("Margins fell {{net_margin:2025->2026}}.", [filing_passage("t")]) == []


# --- a change placeholder is a verb phrase --------------------------------


@pytest.mark.parametrize(
    "draft",
    [
        "{{current_ratio:2025->2026}}.",
        "Liquidity eased: {{current_ratio:2025->2026}}.",
        "Liquidity eased. {{current_ratio:2025->2026}}.",
        "- {{current_ratio:2025->2026}}",
    ],
)
def test_a_change_placeholder_opening_a_clause_is_caught(draft):
    """It renders "fell 0.12x to 1.23x", which needs a subject in front of it."""
    problems = find_problems(draft, METRICS)
    assert problems and "needs a subject" in problems[0]


def test_a_change_placeholder_with_a_subject_is_fine():
    assert find_problems("The current ratio {{current_ratio:2025->2026}}.", METRICS) == []
    assert find_problems("- Net margin {{roe:2025->2026}}, the largest mover.", METRICS) == []


# --- a change placeholder is a verb phrase --------------------------------


@pytest.mark.parametrize(
    "draft",
    [
        "{{current_ratio:2025->2026}}.",
        "Liquidity eased: {{current_ratio:2025->2026}}.",
        "Liquidity eased. {{current_ratio:2025->2026}}.",
        "- {{current_ratio:2025->2026}}",
    ],
)
def test_a_change_placeholder_opening_a_clause_is_caught(draft):
    """It renders "fell 0.12x to 1.23x", which needs a subject in front of it."""
    problems = find_problems(draft, METRICS)
    assert problems and "needs a subject" in problems[0]


def test_a_change_placeholder_with_a_subject_is_fine():
    assert find_problems("The current ratio {{current_ratio:2025->2026}}.", METRICS) == []
    assert find_problems("- Net margin {{roe:2025->2026}}, the largest mover.", METRICS) == []


# --- saying a value twice, and rules of thumb in words ---------------------


def test_a_change_followed_by_its_own_end_value_is_caught():
    """The first live memo on Lambda: "fell 0.12x to 1.23x, leaving it at 1.23x"."""
    draft = "The current ratio {{current_ratio:2025->2026}}, leaving it at {{current_ratio:2026}}."
    problems = find_problems(draft, METRICS)
    assert any("says it twice" in p for p in problems)


@pytest.mark.parametrize(
    "draft",
    [
        # The level in its own sentence is a separate point, not a repeat.
        "The current ratio {{current_ratio:2025->2026}}. At {{current_ratio:2026}}, it is the lowest shown.",
        # The earlier year's level is not repeated by the change.
        "The current ratio, {{current_ratio:2025}} a year earlier, {{current_ratio:2025->2026}}.",
    ],
)
def test_a_level_that_is_not_a_repeat_is_fine(draft):
    assert not any("says it twice" in p for p in find_problems(draft, METRICS))


@pytest.mark.parametrize(
    "draft, phrase",
    [
        ("The quick ratio no longer covers near-term obligations.", "no longer covers"),
        ("The current ratio moved from below to above parity.", "above parity"),
        ("Liquidity remains adequate.", "adequate"),
        ("It is comfortably liquid on an operating-cash basis.", "comfortably"),
    ],
)
def test_a_rule_of_thumb_in_words_is_caught(draft, phrase):
    problems = find_problems(draft, METRICS)
    assert any(f'"{phrase}" judges a ratio' in p for p in problems)


def test_management_s_own_verdict_may_be_cited():
    """In a cited sentence it is the filing's claim, and the claim check reads it."""
    passages = [Passage(id="P1", item="Item 7", text="Cash is adequate for our needs." * 10,
                        ticker="MSFT", accession="acc")]
    draft = "Management considers its cash adequate for its needs [P1]."
    assert not any("judges a ratio" in p for p in find_problems(draft, METRICS, passages=passages))


def test_describing_what_a_ratio_measures_is_not_a_rule_of_thumb():
    draft = "The current ratio (can current assets cover the next year's bills?) {{current_ratio:2025->2026}}."
    assert find_problems(draft, METRICS) == []
