"""Step 2: the ratios.

Nine ratios, each a small function over the facts from `edgar.py`. Claude picks
which ones to use, but never computes one and never sees this file's arithmetic.

Definitions follow Berk & DeMarzo, *Corporate Finance* Ch. 2 and Subramanyam,
*Financial Statement Analysis* Ch. 1 and 10. Balances are ending balances, not
averages (B&D eq. 2.20 does the same; its footnote allows averages instead).
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


def _debt(v: dict[str, float]) -> float:
    """Debt as B&D eq. 2.15: borrowings plus both slices of long-term debt.

    Not total liabilities, which would include payables and deferred revenue.
    Lease liabilities are excluded here; Subramanyam would include them, and
    that variant is Iteration 2.
    """
    return v["short_term_borrowings"] + v["current_long_term_debt"] + v["long_term_debt"]


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
        id="gross_margin",
        description="gross profit over revenue: what's left after the cost of sales",
        unit="percent",
        inputs=("gross_profit", "revenue"),
        denominator="revenue",
        formula=lambda v: v["gross_profit"] / v["revenue"],
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


def compute(metric: Metric, year: int, values: Values) -> MetricResult:
    """One ratio for one year, or a reason there isn't one."""
    missing = [name for name in metric.inputs if name not in values]
    if missing:
        return MetricResult(
            metric_id=metric.id,
            fiscal_year=year,
            value=None,
            unit=metric.unit,
            reason=f"the filing has no {', '.join(missing)} for {year}",
        )

    if metric.needs_any_reported and not any(
        values[name].reported for name in metric.needs_any_reported
    ):
        wanted = " or ".join(metric.needs_any_reported)
        return MetricResult(
            metric_id=metric.id,
            fiscal_year=year,
            value=None,
            unit=metric.unit,
            reason=f"the filing reports no {wanted} for {year} under tags this tool knows",
        )

    numbers = {name: values[name].value for name in metric.inputs}
    denominator = numbers[metric.denominator]
    if denominator == 0:
        return MetricResult(
            metric_id=metric.id,
            fiscal_year=year,
            value=None,
            unit=metric.unit,
            reason=f"{metric.denominator} is zero for {year}",
        )
    if metric.denominator_must_be_positive and denominator < 0:
        return MetricResult(
            metric_id=metric.id,
            fiscal_year=year,
            value=None,
            unit=metric.unit,
            reason=f"{metric.denominator} is negative for {year}, which makes this ratio misleading",
        )

    return MetricResult(
        metric_id=metric.id,
        fiscal_year=year,
        value=metric.formula(numbers),
        unit=metric.unit,
        inputs=numbers,
    )


def compute_all(facts: list[Fact], metric_ids: list[str] | None = None) -> list[MetricResult]:
    """Every requested metric, for every year the filing can support it.

    A year counts only if the metric's denominator is there; otherwise the
    filing simply doesn't cover that year and there is nothing to report.
    """
    metrics = [METRICS_BY_ID[i] for i in (metric_ids or list(METRICS_BY_ID))]
    by_year = values_by_year(facts)

    results = []
    for metric in metrics:
        for year in sorted(by_year, reverse=True):
            if metric.denominator in by_year[year]:
                results.append(compute(metric, year, by_year[year]))
    return results
