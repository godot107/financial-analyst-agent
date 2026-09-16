"""Step 3: placeholders in, memo out.

Claude writes `{{roe:2026}}`; this module decides whether the draft is clean and
then fills in the values. It is the reason a number in the memo cannot have come
from the model.

Placeholder shapes:
    {{metric_id:year}}            -> "30.2%" or "1.23x"
    {{metric_id:year->year}}      -> "rose 0.6 pts to 30.2%"
    {{peer.metric_id:year}}       -> the same, for the company being compared against
"""

import re

from fin_analyst.metrics import MetricResult

PLACEHOLDER = re.compile(r"\{\{(peer\.)?([a-z_]+):(\d{4})(?:->(\d{4}))?\}\}")
# A change placeholder renders its own verb ("fell 0.12x to 1.23x"), so a verb
# typed in front of one reads as "fell fell 0.12x". Drop the writer's word; the
# rendered one is the one guaranteed to match the data.
DOUBLED_VERB = re.compile(
    r"\b(?:rose|fell|grew|climbed|dropped|slipped|increased|decreased|declined|expanded|contracted)\s+"
    r"(\{\{(?:peer\.)?[a-z_]+:\d{4}->\d{4}\}\})",
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


def find_problems(
    draft: str,
    metrics: list[MetricResult],
    peer_metrics: list[MetricResult] = (),
) -> list[str]:
    """Everything wrong with a draft, in the words the writer needs to fix it."""
    available = {"": _results_by_key(metrics), "peer.": _results_by_key(peer_metrics)}
    problems = []

    for match in PLACEHOLDER.finditer(draft):
        prefix, metric_id = match.group(1) or "", match.group(2)
        start_year, end_year = int(match.group(3)), match.group(4)
        whose = "the peer" if prefix else "this company"
        for year in (start_year, int(end_year)) if end_year else (start_year,):
            if (metric_id, year) not in available[prefix]:
                problems.append(
                    f"{match.group(0)} is not a placeholder you were given; "
                    f"there is no {metric_id} for {year} for {whose}"
                )

    problems.extend(_leaked_digits(draft, list(metrics) + list(peer_metrics)))
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
        # "FY2026" is the same allowed fiscal year as "2026": there is no word
        # boundary between Y and 2, so it needs its own pattern or the writer
        # loses a draft to a false positive.
        stripped = re.sub(rf"\bFY\s?{year}\b|\b{year}\b", " ", stripped)

    leaks = re.findall(r"\S*\d[\S]*", stripped)
    return [
        f"'{leak}' is a number you wrote yourself; every number must be a placeholder"
        for leak in dict.fromkeys(leaks)  # unique, in order
    ]


def render(
    draft: str,
    metrics: list[MetricResult],
    footer: str = "",
    peer_metrics: list[MetricResult] = (),
) -> str:
    """Swap every placeholder for its value. Assumes find_problems came back empty."""
    available = {"": _results_by_key(metrics), "peer.": _results_by_key(peer_metrics)}

    def replace(match: re.Match) -> str:
        results = available[match.group(1) or ""]
        metric_id, start_year, end_year = match.group(2), int(match.group(3)), match.group(4)
        if end_year:
            return _format_change(results[(metric_id, start_year)], results[(metric_id, int(end_year))])
        return _format_value(results[(metric_id, start_year)])

    memo = PLACEHOLDER.sub(replace, DOUBLED_VERB.sub(r"\1", draft))
    return f"{memo.rstrip()}\n\n{footer}" if footer else memo


def _source_line(ticker: str, facts) -> str:
    accessions = sorted({f.accession for f in facts})
    periods = sorted({f.period for f in facts if f.line_item == "total_assets"}, reverse=True)
    return f"{ticker}: SEC filing {', '.join(accessions)}; balance sheet dates {', '.join(periods)}."


def build_footer(
    facts,
    metrics: list[MetricResult],
    ticker: str = "Source",
    peer_facts=(),
    peer_ticker: str | None = None,
) -> str:
    """What a reader needs to check the memo, and what to distrust in it."""
    lines = ["---", _source_line(ticker, facts)]
    if peer_ticker:
        lines.append(_source_line(peer_ticker, peer_facts))
    lines.append("Ratios use ending balances, not averages. Debt excludes lease liabilities.")

    if peer_ticker:
        lines.append(
            "The two companies' fiscal years end on different dates, so each is shown at its own "
            "year end rather than at a common one."
        )
    else:
        lines.append("No peer comparison: this memo covers one company against its own prior years.")

    unreported = sorted({f.line_item for f in facts if not f.reported})
    if unreported:
        lines.append(f"{ticker} does not report, so treated as zero: {', '.join(unreported)}.")
    peer_unreported = sorted({f.line_item for f in peer_facts if not f.reported})
    if peer_unreported:
        lines.append(
            f"{peer_ticker} does not report, so treated as zero: {', '.join(peer_unreported)}."
        )
    return "\n".join(lines)
