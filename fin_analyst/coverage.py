"""Which line items were found, which tag matched, and what was treated as zero.

Both real defects this project has hit were tag problems: a fiscal year taken
from the wrong column, and a receivables tag we did not recognise that quietly
became a zero. Neither announced itself; both showed up as a memo that read
slightly wrong. This report makes them visible in seconds, and costs nothing:
no model, no API key.
"""

from pydantic import BaseModel

from fin_analyst.edgar import BALANCE_SHEET_ITEMS, INCOME_AND_CASH_FLOW_ITEMS, OPTIONAL_ITEMS, Fact
from fin_analyst.metrics import METRICS, compute_all

ALL_LINE_ITEMS = {**BALANCE_SHEET_ITEMS, **INCOME_AND_CASH_FLOW_ITEMS}


class LineItemCoverage(BaseModel):
    line_item: str
    concept: str | None  # the tag that matched, if any
    years: list[int]
    status: str  # "found", "treated as zero", or "missing"


def line_item_coverage(facts: list[Fact]) -> list[LineItemCoverage]:
    """One row per line item we look for, found or not."""
    rows = []
    for line_item in ALL_LINE_ITEMS:
        found = [f for f in facts if f.line_item == line_item]
        reported = [f for f in found if f.reported]

        if reported:
            status = "found"
        elif found:
            # Present but zero-filled: fine if the company has none, wrong if we
            # simply failed to recognise its tag.
            status = "treated as zero"
        else:
            status = "missing" if line_item not in OPTIONAL_ITEMS else "missing (optional)"

        rows.append(
            LineItemCoverage(
                line_item=line_item,
                concept=reported[0].concept if reported else None,
                years=sorted({f.fiscal_year for f in found}, reverse=True),
                status=status,
            )
        )
    return rows


class MetricCoverage(BaseModel):
    metric_id: str
    years: list[int]  # years with a value
    unavailable: dict[int, str]  # year -> why not


def metric_coverage(facts: list[Fact]) -> list[MetricCoverage]:
    """Which ratios this filing can support, and why the rest cannot be computed."""
    results = compute_all(facts)
    rows = []
    for metric in METRICS:
        mine = [r for r in results if r.metric_id == metric.id]
        rows.append(
            MetricCoverage(
                metric_id=metric.id,
                years=sorted((r.fiscal_year for r in mine if r.value is not None), reverse=True),
                unavailable={r.fiscal_year: r.reason for r in mine if r.value is None},
            )
        )
    return rows


def concerns(facts: list[Fact]) -> list[str]:
    """The rows worth a human look, in plain words."""
    notes = []
    for row in line_item_coverage(facts):
        if row.status == "treated as zero":
            notes.append(
                f"{row.line_item}: no tag matched, so it counts as zero. Check the filing - "
                "a company that truly has none is fine, a tag we don't know is not."
            )
        elif row.status == "missing":
            notes.append(f"{row.line_item}: not found at all, so metrics needing it are unavailable.")

    for row in metric_coverage(facts):
        if not row.years:
            reasons = "; ".join(sorted(set(row.unavailable.values()))) or "no years computed"
            notes.append(f"{row.metric_id}: no year could be computed ({reasons}).")
    return notes
