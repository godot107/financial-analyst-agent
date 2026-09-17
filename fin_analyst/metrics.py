"""Step 2: the ratios.

The ratios, each a small function over the facts from `edgar.py`. Claude picks
which ones to use, but never computes one and never sees this file's arithmetic.

Definitions follow Berk & DeMarzo, *Corporate Finance* Ch. 2 and Subramanyam,
*Financial Statement Analysis* Ch. 1, 3, 10 and 11. Balances are ending balances,
not averages (B&D eq. 2.20 does the same; its footnote allows averages instead),
except in the one ratio named for its average.
"""

from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from fin_analyst.edgar import Fact

Values = dict[str, Fact]


class MetricResult(BaseModel):
    """One ratio for one year, or the reason there isn't one.

    Pydantic, like Fact: this is what the compute node puts into the graph state.
    """

    model_config = ConfigDict(frozen=True)

    metric_id: str
    fiscal_year: int
    value: float | None
    unit: str  # "ratio" (1.35x) or "percent" (36.1%)
    reason: str | None = None  # why there is no value
    inputs: dict[str, float] = Field(default_factory=dict)


@dataclass(frozen=True)
class Metric:
    id: str
    description: str  # this is what Claude sees when picking metrics
    unit: str
    inputs: tuple[str, ...]
    denominator: str  # checked for zero before dividing
    formula: Callable[[dict[str, float]], float]
    # Equity can be zero or negative after years of buybacks, which makes these
    # ratios misleading rather than merely large.
    denominator_must_be_positive: bool = False
    # Lines we treat as zero when absent are safe only if at least one of a
    # group is actually reported. A company with no commercial paper is real; a
    # company with no receivables usually means we failed to recognise its tag,
    # and zero would quietly overstate the ratio.
    needs_any_reported: tuple[str, ...] = ()
    # A second such group, when a ratio adds two kinds of optional line.
    also_needs_any_reported: tuple[str, ...] = ()
    # Balance-sheet inputs averaged over this year's and last year's end, as
    # Subramanyam computes return on equity. Needs the prior year's balance.
    averaged: tuple[str, ...] = ()


def _debt(v: dict[str, float]) -> float:
    """Debt as B&D eq. 2.15: borrowings plus both slices of long-term debt.

    Not total liabilities, which would include payables and deferred revenue.
    Lease liabilities are excluded here; `_debt_with_leases` is the variant that
    counts them.
    """
    return v["short_term_borrowings"] + v["current_long_term_debt"] + v["long_term_debt"]


def _debt_with_leases(v: dict[str, float]) -> float:
    """Debt plus lease liabilities: Subramanyam treats leases as the financing they are.

    Finance leases a company already includes in a debt line are recorded as 0
    by edgar.py, so nothing is counted twice.
    """
    return _debt(v) + v["operating_lease_liabilities"] + v["finance_lease_liabilities"]


DEBT_LINES = ("short_term_borrowings", "current_long_term_debt", "long_term_debt")
LEASE_LINES = ("operating_lease_liabilities", "finance_lease_liabilities")


def _market_cap(v: dict[str, float]) -> float:
    """The filing's diluted share count at the market's price."""
    return v["share_price"] * v["diluted_shares"]


METRICS: tuple[Metric, ...] = (
    Metric(
        id="current_ratio",
        description="current assets over current liabilities: can it cover the next year's bills",
        unit="ratio",
        inputs=("current_assets", "current_liabilities"),
        denominator="current_liabilities",
        formula=lambda v: v["current_assets"] / v["current_liabilities"],
    ),
    Metric(
        id="quick_ratio",
        description="cash, short-term investments and receivables over current liabilities: a stricter liquidity test",
        unit="ratio",
        inputs=("cash", "short_term_investments", "accounts_receivable", "current_liabilities"),
        denominator="current_liabilities",
        formula=lambda v: (v["cash"] + v["short_term_investments"] + v["accounts_receivable"])
        / v["current_liabilities"],
        needs_any_reported=("short_term_investments", "accounts_receivable"),
    ),
    Metric(
        id="cash_flow_ratio",
        description="operating cash flow over current liabilities: liquidity from the cash the business generates",
        unit="ratio",
        inputs=("operating_cash_flow", "current_liabilities"),
        denominator="current_liabilities",
        formula=lambda v: v["operating_cash_flow"] / v["current_liabilities"],
    ),
    Metric(
        id="debt_to_equity",
        description="borrowings and long-term debt over shareholders' equity: leverage",
        unit="ratio",
        inputs=("short_term_borrowings", "current_long_term_debt", "long_term_debt", "equity"),
        denominator="equity",
        formula=lambda v: _debt(v) / v["equity"],
        denominator_must_be_positive=True,
        needs_any_reported=("short_term_borrowings", "current_long_term_debt", "long_term_debt"),
    ),
    Metric(
        id="debt_to_equity_with_leases",
        description="borrowings, long-term debt and lease liabilities over shareholders' equity: leverage counting leases as debt",
        unit="ratio",
        inputs=(*DEBT_LINES, *LEASE_LINES, "equity"),
        denominator="equity",
        formula=lambda v: _debt_with_leases(v) / v["equity"],
        denominator_must_be_positive=True,
        needs_any_reported=DEBT_LINES,
        also_needs_any_reported=LEASE_LINES,
    ),
    Metric(
        id="debt_to_capital",
        description="borrowings and long-term debt over debt plus shareholders' equity: the share of capital that is borrowed",
        unit="percent",
        inputs=(*DEBT_LINES, "equity"),
        denominator="equity",
        formula=lambda v: _debt(v) / (_debt(v) + v["equity"]),
        denominator_must_be_positive=True,
        needs_any_reported=DEBT_LINES,
    ),
    Metric(
        id="interest_coverage",
        description="operating income (EBIT) over interest expense: how many times earnings cover the interest bill",
        unit="ratio",
        inputs=("operating_income", "interest_expense"),
        denominator="interest_expense",
        formula=lambda v: v["operating_income"] / v["interest_expense"],
        denominator_must_be_positive=True,
    ),
    Metric(
        id="gross_margin",
        description="gross profit over revenue: what's left after the cost of sales",
        unit="percent",
        inputs=("gross_profit", "revenue"),
        denominator="revenue",
        formula=lambda v: v["gross_profit"] / v["revenue"],
    ),
    Metric(
        id="operating_margin",
        description="operating income over revenue: profit from the business before interest and taxes",
        unit="percent",
        inputs=("operating_income", "revenue"),
        denominator="revenue",
        formula=lambda v: v["operating_income"] / v["revenue"],
    ),
    Metric(
        id="net_margin",
        description="net income over revenue: profit per dollar of sales, the first DuPont component",
        unit="percent",
        inputs=("net_income", "revenue"),
        denominator="revenue",
        formula=lambda v: v["net_income"] / v["revenue"],
    ),
    Metric(
        id="asset_turnover",
        description="revenue over total assets: sales per dollar of assets, the second DuPont component",
        unit="ratio",
        inputs=("revenue", "total_assets"),
        denominator="total_assets",
        formula=lambda v: v["revenue"] / v["total_assets"],
    ),
    Metric(
        id="equity_multiplier",
        description="total assets over equity: how much leverage lifts returns, the third DuPont component",
        unit="ratio",
        inputs=("total_assets", "equity"),
        denominator="equity",
        formula=lambda v: v["total_assets"] / v["equity"],
        denominator_must_be_positive=True,
    ),
    Metric(
        id="roe",
        description="net income over equity: return on equity, equal to net margin x asset turnover x equity multiplier",
        unit="percent",
        inputs=("net_income", "equity"),
        denominator="equity",
        formula=lambda v: v["net_income"] / v["equity"],
        denominator_must_be_positive=True,
    ),
    Metric(
        id="roe_average_equity",
        description="net income over the average of this and last year's equity: return on equity as Subramanyam computes it, less distorted by a year-end buyback or issue",
        unit="percent",
        inputs=("net_income", "equity"),
        denominator="equity",
        formula=lambda v: v["net_income"] / v["equity"],
        denominator_must_be_positive=True,
        averaged=("equity",),
    ),
    Metric(
        id="pe_ratio",
        description="market value of the company over its net income: what the market pays for a dollar of earnings (price is current, earnings are the fiscal year's)",
        unit="ratio",
        inputs=("share_price", "diluted_shares", "net_income"),
        denominator="net_income",
        formula=lambda v: _market_cap(v) / v["net_income"],
        denominator_must_be_positive=True,
    ),
    Metric(
        id="market_to_book",
        description="market value of the company over its book equity: how far the market values it above its accounts",
        unit="ratio",
        inputs=("share_price", "diluted_shares", "equity"),
        denominator="equity",
        formula=lambda v: _market_cap(v) / v["equity"],
        denominator_must_be_positive=True,
    ),
    Metric(
        id="ev_to_revenue",
        description="enterprise value (market value plus debt, less cash) over revenue: the whole firm's price per dollar of sales",
        unit="ratio",
        inputs=(
            "share_price",
            "diluted_shares",
            "short_term_borrowings",
            "current_long_term_debt",
            "long_term_debt",
            "cash",
            "revenue",
        ),
        denominator="revenue",
        formula=lambda v: (_market_cap(v) + _debt(v) - v["cash"]) / v["revenue"],
    ),
)

METRICS_BY_ID = {m.id: m for m in METRICS}
# Inputs read from a balance sheet: a ratio using any of them covers balance sheet years.
BALANCE_INPUTS = {
    "total_assets", "current_assets", "current_liabilities", "cash", "short_term_investments",
    "accounts_receivable", "equity", "short_term_borrowings", "current_long_term_debt",
    "long_term_debt", "operating_lease_liabilities", "finance_lease_liabilities",
}
# Ratios that need a share price, which no filing contains.
MARKET_METRIC_IDS = {m.id for m in METRICS if "share_price" in m.inputs}


def describe_metrics() -> str:
    """The list Claude picks from."""
    return "\n".join(f"- {m.id}: {m.description}" for m in METRICS)


def values_by_year(facts: list[Fact]) -> dict[int, Values]:
    by_year: dict[int, Values] = {}
    for fact in facts:
        by_year.setdefault(fact.fiscal_year, {})[fact.line_item] = fact
    return by_year


# Lines only a classified balance sheet or a commercial income statement has.
# The debt lines belong here too: a bank's debt isn't split into current and
# long-term, so these tags find only a sliver of it. JPMorgan came out at 0.18x
# debt to equity and Travelers at 0.00x - wrong, and plausible enough to publish.
CLASSIFIED_ONLY = {
    "current_assets", "current_liabilities", "operating_income", "interest_expense",
    "short_term_borrowings", "current_long_term_debt", "long_term_debt",
}
NOT_APPLICABLE = (
    "doesn't apply: the balance sheet isn't split into current and non-current and there is no "
    "operating income line, as is usual for banks and insurers"
)


def _unavailable(metric: Metric, year: int, reason: str) -> MetricResult:
    return MetricResult(metric_id=metric.id, fiscal_year=year, value=None, unit=metric.unit, reason=reason)


def compute(metric: Metric, year: int, values: Values, prior: Values | None = None) -> MetricResult:
    """One ratio for one year, or a reason there isn't one.

    `prior` is the year before, needed only by a ratio with averaged inputs.
    """
    missing = [name for name in metric.inputs if name not in values]
    if missing:
        return _unavailable(metric, year, f"the filing has no {', '.join(missing)} for {year}")

    for group in (metric.needs_any_reported, metric.also_needs_any_reported):
        if group and not any(values[name].reported for name in group):
            wanted = " or ".join(group)
            return _unavailable(
                metric, year, f"the filing reports no {wanted} for {year} under tags this tool knows"
            )

    numbers = {name: values[name].value for name in metric.inputs}
    for name in metric.averaged:
        if prior is None or name not in prior:
            return _unavailable(
                metric, year, f"averaging {name} needs {year - 1}'s balance, which these filings don't include"
            )
        numbers[f"{name}_prior_year"] = prior[name].value
        numbers[name] = (values[name].value + prior[name].value) / 2

    denominator = numbers[metric.denominator]
    if denominator == 0:
        return _unavailable(metric, year, f"{metric.denominator} is zero for {year}")
    if metric.denominator_must_be_positive and denominator < 0:
        return _unavailable(
            metric, year, f"{metric.denominator} is negative for {year}, which makes this ratio misleading"
        )

    return MetricResult(
        metric_id=metric.id,
        fiscal_year=year,
        value=metric.formula(numbers),
        unit=metric.unit,
        inputs=numbers,
    )


def is_unclassified(facts: list[Fact]) -> bool:
    """No current assets or liabilities in any year: a bank's or insurer's balance sheet.

    They list assets and liabilities by liquidity instead of splitting them at
    one year, and report no operating income, so the liquidity ratios and
    interest coverage don't apply. JPMorgan's and Travelers' 10-Ks both look so.
    """
    items = {f.line_item for f in facts}
    # Only a real balance sheet counts: a hand-built set of facts with no total
    # assets says nothing about how the company classifies its balance sheet.
    return "total_assets" in items and not items & {"current_assets", "current_liabilities"}


def _years_for(metric: Metric, by_year: dict[int, Values]) -> list[int]:
    """The years a metric should report on, newest first.

    Every year it can actually be computed, plus every year of the statement it
    belongs to - a balance-sheet ratio every year with a balance sheet, an income
    ratio every year with revenue - so a line that stops being reported shows up
    as "not available" for that year. Apple stopped reporting interest expense
    after 2023: without this, its interest coverage simply ended in 2023, and
    read as current.
    """
    anchor = "total_assets" if BALANCE_INPUTS & set(metric.inputs) else "revenue"
    if not any(anchor in values for values in by_year.values()):
        anchor = metric.denominator  # a partial set of facts: fall back to what is there
    years = sorted(
        (
            year for year, values in by_year.items()
            if anchor in values or all(name in values for name in metric.inputs)
        ),
        reverse=True,
    )
    if metric.id in MARKET_METRIC_IDS:
        return years[:1]  # a price exists only for today
    if metric.averaged:
        # Starts where there is a year before to average with.
        years = [
            y for y in years
            if y - 1 in by_year and all(n in by_year[y - 1] for n in metric.averaged)
        ]
    return years


def compute_all(facts: list[Fact], metric_ids: list[str] | None = None) -> list[MetricResult]:
    """Every requested metric, for every year the filings cover.

    A metric that can't be computed still gets a result saying why, so the
    writer tells the reader instead of silently dropping what was asked for.
    """
    metrics = [METRICS_BY_ID[i] for i in (metric_ids or list(METRICS_BY_ID))]
    by_year = values_by_year(facts)
    if not by_year:
        return []
    latest = max(by_year)
    unclassified = is_unclassified(facts)

    results = []
    for metric in metrics:
        if unclassified and CLASSIFIED_ONLY & set(metric.inputs):
            results.append(_unavailable(metric, latest, NOT_APPLICABLE))
            continue
        years = _years_for(metric, by_year)
        if not years:
            reason = (
                "averaging needs two balance sheets in a row, which these filings don't include"
                if metric.averaged
                else f"the filing has no {metric.denominator} for any year"
            )
            results.append(_unavailable(metric, latest, reason))
            continue
        results += [compute(metric, year, by_year[year], by_year.get(year - 1)) for year in years]
    return results
