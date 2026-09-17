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
from fin_analyst.news import news_credit
from fin_analyst.passages import Passage

PLACEHOLDER = re.compile(r"\{\{(peer\.)?([a-z_]+):(\d{4})(?:->(\d{4}))?\}\}")
# A change placeholder renders its own verb ("fell 0.12x to 1.23x"), so a verb
# typed in front of one reads as "fell fell 0.12x". Drop the writer's word; the
# rendered one is the one guaranteed to match the data.
DOUBLED_VERB = re.compile(
    r"\b(?:rose|fell|grew|climbed|dropped|slipped|increased|decreased|declined|expanded|contracted)\s+"
    r"(\{\{(?:peer\.)?[a-z_]+:\d{4}->\d{4}\}\})",
    re.IGNORECASE,
)
# A citation of a filing passage, e.g. [P3]. The digits inside are an id, not a
# figure, so they are stripped before the leak check looks for numbers.
CITATION = re.compile(r"\[([PN]\d+)\]")  # P = the filing, N = news
# Allowed in prose despite containing digits: the form name and fiscal years,
# which the check adds from the data it was given.
ALWAYS_ALLOWED = {"10-K", "10-Q", "8-K"}
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _dates_in(passages: list[Passage]) -> set[str]:
    """The dates the passages themselves carry.

    News must be dated - a memo saying what a company announced without saying
    when is worse than useless - so those dates have to survive a check that
    otherwise rejects every digit. Only dates the sources actually carry pass.
    """
    dates = set()
    for passage in passages:
        dates.update(re.findall(r"\d{4}-\d{2}-\d{2}", f"{passage.published or ''} {passage.item}"))
    return dates


def _strip_dates(text: str, dates: set[str]) -> str:
    for iso in dates:
        year, month, day = iso.split("-")
        name = MONTHS[int(month) - 1]
        for written in (
            iso,
            f"{name} {int(day)}, {year}",
            f"{name} {int(day)} {year}",
            f"{int(day)} {name} {year}",
        ):
            text = text.replace(written, " ")
    return text


def _results_by_key(metrics: list[MetricResult]) -> dict[tuple[str, int], MetricResult]:
    return {(m.metric_id, m.fiscal_year): m for m in metrics}


def format_value(result: MetricResult) -> str:
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

    # Compare what will be printed, not the raw difference: a change of 0.003x
    # would otherwise render as "rose 0.00x to 0.67x" (Apple's cash flow ratio).
    if size in ("0.00x", "0.0 pts"):
        return f"was unchanged at {format_value(end)}"
    direction = "rose" if change > 0 else "fell"
    return f"{direction} {size} to {format_value(end)}"


def find_problems(
    draft: str,
    metrics: list[MetricResult],
    peer_metrics: list[MetricResult] = (),
    passages: list[Passage] = (),
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

    # A change placeholder renders "fell 0.12x to 1.23x", a verb phrase. Where a
    # sentence or clause opens with one, the memo reads "Capacity shows in the
    # asset base: fell 0.02x to 0.44x" - there is no subject for the verb.
    clause_start = r"(?:^\s*(?:[-*]\s*)?|[.!?:;]\s*|\n\s*(?:[-*]\s*)?)"
    change = r"(\{\{(?:peer\.)?[a-z_]+:\d{4}->\d{4}\}\})"
    for match in re.finditer(clause_start + change, draft):
        problems.append(
            f"{match.group(1)} opens a clause, but it renders a verb phrase "
            '("fell 0.12x to 1.23x"), so it needs a subject in front of it'
        )

    problems.extend(_repeated_values(draft))
    problems.extend(_rules_of_thumb(draft))

    known = {p.id for p in passages}
    for cited in dict.fromkeys(CITATION.findall(draft)):
        if cited not in known:
            problems.append(
                f"[{cited}] is not a passage you were given; cite only the ones listed"
            )

    problems.extend(_leaked_digits(draft, list(metrics) + list(peer_metrics), passages))
    return problems


def _sentences(draft: str) -> list[str]:
    """Sentences and list items; placeholders contain no sentence punctuation."""
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", draft) if s.strip()]


def _repeated_values(draft: str) -> list[str]:
    """A change followed by its own end value: "fell 0.12x to 1.23x, leaving it at 1.23x".

    The change placeholder already ends on the value, so the second one only
    repeats it. The prompt showed exactly this as a bad example, and the first
    live memo on Lambda did it anyway, so it is checked rather than asked for.
    """
    problems = []
    for sentence in _sentences(draft):
        levels = {(m.group(1) or "", m.group(2), m.group(3)) for m in PLACEHOLDER.finditer(sentence) if not m.group(4)}
        for m in PLACEHOLDER.finditer(sentence):
            prefix, metric_id, end = m.group(1) or "", m.group(2), m.group(4)
            if end and (prefix, metric_id, end) in levels:
                problems.append(
                    f"{m.group(0)} already ends on the {end} value, so "
                    f"{{{{{prefix}{metric_id}:{end}}}}} in the same sentence says it twice; remove one"
                )
    return problems


# Judging a ratio against a benchmark the data doesn't contain: "above parity",
# "no longer covers its obligations", "adequate". The digit check catches
# "above 2x"; these are the same judgment written in words.
RULE_OF_THUMB = [
    re.compile(
        r"\b(?:above|below|under|over|less than|more than|greater than|at least|short of)\s+"
        r"(?:one|two|three|parity|unity)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:no longer|not|fails? to|unable to|cannot|can't|does not|doesn't|fully|more than|"
        r"less than|barely|easily)\s+(?:fully\s+)?cover(?:s|ed|ing)?\b",
        re.IGNORECASE,
    ),
]
# Verdicts that need a standard to mean anything. Allowed in a sentence that
# cites a passage, where they are management's words and the claim check reads them.
VERDICT_WORDS = re.compile(
    r"\b(?:healthy|unhealthy|adequate|inadequate|comfortable|comfortably)\b", re.IGNORECASE
)


def _rules_of_thumb(draft: str) -> list[str]:
    problems = []
    for sentence in _sentences(draft):
        found = [m.group(0) for pattern in RULE_OF_THUMB for m in pattern.finditer(sentence)]
        if not CITATION.search(sentence):
            found += [m.group(0) for m in VERDICT_WORDS.finditer(sentence)]
        for phrase in dict.fromkeys(found):
            problems.append(
                f'"{phrase}" judges a ratio against a standard the data does not contain; '
                "describe the change against the prior year instead"
            )
    return problems


# A name that happens to contain digits: "Microsoft 365", "Item 1A", "401k", or a
# term like "53-week" (Costco's fiscal 2023). Never a measurement, which always
# carries a decimal point, a currency symbol or a percent sign.
NAME_LIKE = re.compile(r"^[A-Za-z]*\d+[A-Za-z]*(?:-[A-Za-z]+)?$")
# The 10-K's own section names. "Item 7" cost Costco's memo a draft: the passage
# text never contains the item number, only its label does.
SECTION = re.compile(r"\bItems? \d{1,2}[A-C]?\b")


def _is_a_name_from_the_filing(token: str, passages: list[Passage]) -> bool:
    """True for a product or section name the filing itself uses.

    "Microsoft 365" is the company's name for a product, not a figure, and
    blocking it costs a retry for nothing. A measurement never passes this: it
    carries a decimal, a currency symbol or a percent sign, so NAME_LIKE rejects
    it whether or not the passage contains it.
    """
    word = token.strip(".,;:()[]'\"")
    if not NAME_LIKE.match(word):
        return False
    return any(re.search(rf"\b{re.escape(word)}\b", p.text) for p in passages)


def _leaked_digits(
    draft: str, metrics: list[MetricResult], passages: list[Passage] = ()
) -> list[str]:
    """Any digit the model typed itself.

    Placeholders and citations are removed first, then the fiscal years present
    in the data and the form names, and whatever digits remain were written by
    Claude - except names the filing itself uses.
    """
    stripped = SECTION.sub(" ", CITATION.sub(" ", PLACEHOLDER.sub(" ", draft)))
    stripped = _strip_dates(stripped, _dates_in(list(passages)))
    for allowed in ALWAYS_ALLOWED:
        stripped = stripped.replace(allowed, " ")
    for year in {str(m.fiscal_year) for m in metrics}:
        # "FY2026" is the same allowed fiscal year as "2026": there is no word
        # boundary between Y and 2, so it needs its own pattern or the writer
        # loses a draft to a false positive.
        stripped = re.sub(rf"\bFY\s?{year}\b|\b{year}\b", " ", stripped)

    leaks = [
        leak
        for leak in dict.fromkeys(re.findall(r"\S*\d[\S]*", stripped))  # unique, in order
        if not _is_a_name_from_the_filing(leak, passages)
    ]
    return [
        f"'{leak}' is a number you wrote yourself; every number must be a placeholder"
        for leak in leaks
    ]


def render(
    draft: str,
    metrics: list[MetricResult],
    footer: str = "",
    peer_metrics: list[MetricResult] = (),
    passages: list[Passage] = (),
) -> str:
    """Swap every placeholder for its value. Assumes find_problems came back empty."""
    available = {"": _results_by_key(metrics), "peer.": _results_by_key(peer_metrics)}

    def replace(match: re.Match) -> str:
        results = available[match.group(1) or ""]
        metric_id, start_year, end_year = match.group(2), int(match.group(3)), match.group(4)
        if end_year:
            return _format_change(results[(metric_id, start_year)], results[(metric_id, int(end_year))])
        return format_value(results[(metric_id, start_year)])

    memo = PLACEHOLDER.sub(replace, DOUBLED_VERB.sub(r"\1", draft))
    sources = build_sources(memo, passages)
    if sources:
        memo = f"{memo.rstrip()}\n\n{sources}"
    return f"{memo.rstrip()}\n\n{footer}" if footer else memo


def cited_claims(draft: str, passages: list[Passage] = ()) -> list[tuple[str, list[Passage]]]:
    """Each sentence that cites the filing, with the passages it cites.

    This is what the claim check reads. Placeholders are left as they are: the
    judge is asked whether the passage supports the claim, not whether a figure
    is right - the figures are already guaranteed.
    """
    by_id = {p.id: p for p in passages}
    claims = []
    for sentence in re.split(r"(?<=[.!?])\s+", draft):
        cited = [by_id[i] for i in dict.fromkeys(CITATION.findall(sentence)) if i in by_id]
        if cited:
            claims.append((" ".join(sentence.split()), cited))
    return claims


def build_sources(memo: str, passages: list[Passage], quote_chars: int = 220) -> str:
    """List every cited passage, so a reader can check the claim against the filing."""
    by_id = {p.id: p for p in passages}
    cited = [by_id[i] for i in dict.fromkeys(CITATION.findall(memo)) if i in by_id]
    if not cited:
        return ""

    lines = ["**Cited sources**"]
    for passage in cited:
        quote = passage.text[:quote_chars].rstrip()
        if len(passage.text) > quote_chars:
            quote += "..."
        where = passage.url or f"filing {passage.accession}"
        lines.append(f'- [{passage.id}] {passage.item}, {where}: "{quote}"')
    return "\n".join(lines)


def _source_line(ticker: str, facts) -> str:
    accessions = sorted({f.accession for f in facts})
    periods = sorted({f.period for f in facts if f.line_item == "total_assets"}, reverse=True)
    filings = "SEC filing" if len(accessions) == 1 else "SEC filings"
    return f"{ticker}: {filings} {', '.join(accessions)}; balance sheet dates {', '.join(periods)}."


def _restatements(ticker: str, facts) -> str | None:
    """Figures a later 10-K changed. The memo uses the later figure."""
    changed = [f for f in facts if f.earlier_value is not None]
    if not changed:
        return None
    items = "; ".join(
        f"{f.line_item} {f.fiscal_year} (first filed in {f.earlier_accession})"
        for f in sorted(changed, key=lambda f: (f.line_item, -f.fiscal_year))
    )
    return (
        f"{ticker} figures a later filing changed, restated or tagged differently; the later "
        f"figure is used: {items}."
    )


def build_footer(
    facts,
    metrics: list[MetricResult],
    ticker: str = "Source",
    peer_facts=(),
    peer_ticker: str | None = None,
    passages: list[Passage] = (),
) -> str:
    """What a reader needs to check the memo, and what to distrust in it."""
    lines = ["---", _source_line(ticker, facts)]
    if peer_ticker:
        lines.append(_source_line(peer_ticker, peer_facts))
    used = {m.metric_id for m in metrics}
    lines.append(
        "Ratios use ending balances, not averages"
        + (", except roe_average_equity." if "roe_average_equity" in used else ".")
        + " Debt excludes lease liabilities"
        + (", except in debt_to_equity_with_leases." if "debt_to_equity_with_leases" in used else ".")
    )
    for who, their_facts in ((ticker, facts), (peer_ticker, peer_facts)):
        note = _restatements(who, their_facts) if who else None
        if note:
            lines.append(note)

    price = next((f for f in facts if f.line_item == "share_price"), None)
    if price:
        lines.append(
            f"Share price from {price.accession}. Valuation ratios put that price against the "
            "fiscal year's figures, so they are as of that date, not the year end."
        )

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
    credit = news_credit(passages)
    if credit:
        lines.append(credit)

    peer_unreported = sorted({f.line_item for f in peer_facts if not f.reported})
    if peer_unreported:
        lines.append(
            f"{peer_ticker} does not report, so treated as zero: {', '.join(peer_unreported)}."
        )
    return "\n".join(lines)
