"""Step 3: placeholders in, memo out.

Claude writes `{{roe:2026}}`; this module decides whether the draft is clean and
then fills in the values. It is the reason a number in the memo cannot have come
from the model.

Two placeholder shapes:
    {{metric_id:year}}            -> "30.2%" or "1.23x"
    {{metric_id:year->year}}      -> "rose 0.6 pts to 30.2%"
"""

import re

from fin_analyst.metrics import MetricResult

PLACEHOLDER = re.compile(r"\{\{([a-z_]+):(\d{4})(?:->(\d{4}))?\}\}")
# A change placeholder renders its own verb ("fell 0.12x to 1.23x"), so a verb
# typed in front of one reads as "fell fell 0.12x". Drop the writer's word; the
# rendered one is the one guaranteed to match the data.
DOUBLED_VERB = re.compile(
    r"\b(?:rose|fell|grew|climbed|dropped|slipped|increased|decreased|declined|expanded|contracted)\s+"
    r"(\{\{[a-z_]+:\d{4}->\d{4}\}\})",
    re.IGNORECASE,
)
# Allowed in prose despite containing digits: the form name and fiscal years,
# which the check adds from the data it was given.
ALWAYS_ALLOWED = {"10-K", "10-Q", "8-K"}


def _results_by_key(metrics: list[MetricResult]) -> dict[tuple[str, int], MetricResult]:
    return {(m.metric_id, m.fiscal_year): m for m in metrics}


def _format_value(result: MetricResult) -> str:
    if result.value is None:
        return f"not available ({result.reason})"
    if result.unit == "percent":
        return f"{result.value * 100:.1f}%"
    return f"{result.value:.2f}x"


def _format_change(start: MetricResult, end: MetricResult) -> str:
    """The direction is rendered, not written, so the model can't get it backwards."""
    if start.value is None or end.value is None:
        missing = start if start.value is None else end
        return f"not available ({missing.reason})"

    if end.unit == "percent":
        change = (end.value - start.value) * 100
        size = f"{abs(change):.1f} pts"
    else:
        change = end.value - start.value
        size = f"{abs(change):.2f}x"

    if abs(change) < 1e-9:
        return f"was unchanged at {_format_value(end)}"
    direction = "rose" if change > 0 else "fell"
    return f"{direction} {size} to {_format_value(end)}"


def find_problems(draft: str, metrics: list[MetricResult]) -> list[str]:
    """Everything wrong with a draft, in the words the writer needs to fix it."""
    results = _results_by_key(metrics)
    problems = []

    for match in PLACEHOLDER.finditer(draft):
        metric_id, start_year, end_year = match.group(1), int(match.group(2)), match.group(3)
        for year in (start_year, int(end_year)) if end_year else (start_year,):
            if (metric_id, year) not in results:
                problems.append(
                    f"{match.group(0)} is not a placeholder you were given; "
                    f"there is no {metric_id} for {year}"
                )

    problems.extend(_leaked_digits(draft, metrics))
    return problems


def _leaked_digits(draft: str, metrics: list[MetricResult]) -> list[str]:
    """Any digit the model typed itself.

    Placeholders are removed first, then the fiscal years present in the data and
    the form names are removed, and whatever digits remain were written by Claude.
    """
    stripped = PLACEHOLDER.sub(" ", draft)
    for allowed in ALWAYS_ALLOWED:
        stripped = stripped.replace(allowed, " ")
    for year in {str(m.fiscal_year) for m in metrics}:
        stripped = re.sub(rf"\b{year}\b", " ", stripped)

    leaks = re.findall(r"\S*\d[\S]*", stripped)
    return [
        f"'{leak}' is a number you wrote yourself; every number must be a placeholder"
        for leak in dict.fromkeys(leaks)  # unique, in order
    ]


def render(draft: str, metrics: list[MetricResult], footer: str = "") -> str:
    """Swap every placeholder for its value. Assumes find_problems came back empty."""
    results = _results_by_key(metrics)

    def replace(match: re.Match) -> str:
        metric_id, start_year, end_year = match.group(1), int(match.group(2)), match.group(3)
        if end_year:
            return _format_change(results[(metric_id, start_year)], results[(metric_id, int(end_year))])
        return _format_value(results[(metric_id, start_year)])

    memo = PLACEHOLDER.sub(replace, DOUBLED_VERB.sub(r"\1", draft))
    return f"{memo.rstrip()}\n\n{footer}" if footer else memo


def build_footer(facts, metrics: list[MetricResult]) -> str:
    """What a reader needs to check the memo, and what to distrust in it."""
    accessions = sorted({f.accession for f in facts})
    periods = sorted({f.period for f in facts if f.line_item == "total_assets"}, reverse=True)
    unreported = sorted({f.line_item for f in facts if not f.reported})

    lines = [
        "---",
        f"Source: SEC filing {', '.join(accessions)}; balance sheet dates {', '.join(periods)}.",
        "Ratios use ending balances, not averages. Debt excludes lease liabilities.",
        "No peer comparison: this memo covers one company against its own prior years.",
    ]
    if unreported:
        lines.append(f"Not reported by the company, treated as zero: {', '.join(unreported)}.")
    return "\n".join(lines)
