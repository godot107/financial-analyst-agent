"""Step 1: turn a company's latest 10-K into a list of facts.

No LLM here, and no ratios either. This module only finds numbers in the filing
and records where each one came from.
"""

import os
from pathlib import Path

import pandas as pd
from edgar import Company, set_identity
from pydantic import BaseModel, ConfigDict, TypeAdapter


class Fact(BaseModel):
    """One number from a filing, with where it came from.

    Pydantic rather than a plain dataclass: these values travel through the
    LangGraph state, and validation there catches a malformed value at the node
    that produced it instead of three nodes later.
    """

    model_config = ConfigDict(frozen=True)

    line_item: str  # our name, e.g. "current_assets"
    fiscal_year: int
    value: float
    concept: str  # the XBRL tag it came from, e.g. "us-gaap:AssetsCurrent"
    period: str  # balance sheet date, or the last day of the income statement year
    accession: str  # the filing it came from
    reported: bool = True  # False = not on the statement, treated as 0


FactList = TypeAdapter(list[Fact])


# Candidate XBRL tags per line item, best first. Companies tag the same idea
# differently, so we try each in turn and record which one matched.
BALANCE_SHEET_ITEMS = {
    "total_assets": ["us-gaap:Assets"],
    "liabilities_and_equity": ["us-gaap:LiabilitiesAndStockholdersEquity"],
    "current_assets": ["us-gaap:AssetsCurrent"],
    "current_liabilities": ["us-gaap:LiabilitiesCurrent"],
    "cash": [
        "us-gaap:CashAndCashEquivalentsAtCarryingValue",
        "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "short_term_investments": [
        "us-gaap:ShortTermInvestments",
        "us-gaap:MarketableSecuritiesCurrent",
        "us-gaap:AvailableForSaleSecuritiesDebtSecuritiesCurrent",
    ],
    "accounts_receivable": [
        "us-gaap:AccountsReceivableNetCurrent",
        "us-gaap:ReceivablesNetCurrent",
        "us-gaap:AccountsAndOtherReceivablesNetCurrent",
    ],
    # Parent-company equity: the same owners as net income below.
    "equity": ["us-gaap:StockholdersEquity"],
    "short_term_borrowings": [
        "us-gaap:ShortTermBorrowings",
        "us-gaap:CommercialPaper",
        "us-gaap:OtherShortTermBorrowings",
    ],
    "current_long_term_debt": [
        "us-gaap:LongTermDebtCurrent",
        "us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent",
    ],
    "long_term_debt": [
        "us-gaap:LongTermDebtNoncurrent",
        "us-gaap:LongTermDebtAndCapitalLeaseObligationsNoncurrent",
    ],
}

INCOME_AND_CASH_FLOW_ITEMS = {
    "revenue": [
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:Revenues",
        "us-gaap:SalesRevenueNet",
    ],
    "gross_profit": ["us-gaap:GrossProfit"],
    # Parent-company net income. ProfitLoss would include minority interests.
    "net_income": ["us-gaap:NetIncomeLoss"],
    "operating_cash_flow": [
        "us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    # Diluted weighted-average shares, not the cover page's share count: this one
    # belongs to a fiscal year, so it lines up with that year's earnings. The
    # cover page count is dated after the year end and would land in the wrong
    # year for a December filer.
    "diluted_shares": [
        "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
        "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic",
    ],
}

# Lines a company may simply not have. Absent means zero, not unknown.
OPTIONAL_ITEMS = {
    "short_term_investments",
    "accounts_receivable",
    "short_term_borrowings",
    "current_long_term_debt",
    "long_term_debt",
}


def _undimensioned(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only whole-company facts.

    A filing also tags the same concept broken down by segment, equity component
    and so on. Those rows carry a dimension and must not be mistaken for the total.
    """
    return df[(~df["is_dimensioned"].fillna(False)) & df["numeric_value"].notna()]


def _pick(df: pd.DataFrame, concepts: list[str]) -> pd.DataFrame:
    """Rows for the first concept in the list that appears at all."""
    for concept in concepts:
        rows = df[df["concept"] == concept]
        if not rows.empty:
            return rows
    return df.iloc[0:0]


def select_facts(df: pd.DataFrame, accession: str) -> list[Fact]:
    """Pick our line items out of a filing's facts table."""
    df = _undimensioned(df)
    instants = df[df["period_type"] == "instant"]
    # A 10-K also tags quarterly durations; keep the annual ones.
    durations = df[(df["period_type"] == "duration") & (df["fiscal_period"] == "FY")]

    facts: list[Fact] = []
    for items, rows, period_column in (
        (BALANCE_SHEET_ITEMS, instants, "period_instant"),
        (INCOME_AND_CASH_FLOW_ITEMS, durations, "period_end"),
    ):
        for line_item, concepts in items.items():
            picked = _pick(rows, concepts)
            for _, row in picked.iterrows():
                period = str(row[period_column])
                facts.append(
                    Fact(
                        line_item=line_item,
                        fiscal_year=fiscal_year_of(period),
                        value=float(row["numeric_value"]),
                        concept=str(row["concept"]),
                        period=period,
                        accession=accession,
                    )
                )

    return _fill_optional(facts, instants, accession)


def fiscal_year_of(period: str) -> int:
    """The fiscal year a period belongs to, taken from its end date.

    The filing's own fiscal_year column cannot be trusted. Costco's year ends in
    late August, and its FY2024 figures - both the income statement ending
    2024-09-01 and the balance sheet dated the same day - arrive labelled 2025.
    Last year's numbers then overwrite this year's under one key.

    Known limit: a year ending in the first days of January (some 52/53-week
    retailers) is dated to the new calendar year, which is a year later than the
    company calls it. The memo footer always shows the period end date, so the
    ambiguity is visible rather than hidden.
    """
    return int(period[:4])


def _fill_optional(facts: list[Fact], instants: pd.DataFrame, accession: str) -> list[Fact]:
    """Record optional balance-sheet lines the company doesn't report as 0.

    A company with no commercial paper has no commercial paper line. That is a
    zero, not a missing input, so the ratio should still compute. The `reported`
    flag keeps it visible: the memo footer lists these.
    """
    known = {(f.line_item, f.fiscal_year) for f in facts}
    balance_dates = {f.fiscal_year: f.period for f in facts if f.line_item == "total_assets"}

    for line_item in sorted(OPTIONAL_ITEMS):
        for fiscal_year, period in sorted(balance_dates.items()):
            if (line_item, fiscal_year) not in known:
                facts.append(
                    Fact(
                        line_item=line_item,
                        fiscal_year=fiscal_year,
                        value=0.0,
                        concept="",
                        period=period,
                        accession=accession,
                        reported=False,
                    )
                )
    return sorted(facts, key=lambda f: (f.line_item, -f.fiscal_year))


def fetch_facts(ticker: str, identity: str | None = None) -> list[Fact]:
    """Download the company's latest 10-K and pull our line items out of it."""
    identity = identity or os.environ.get("SEC_USER_AGENT")
    if not identity:
        raise RuntimeError(
            "SEC_USER_AGENT is not set. The SEC requires a name and email on every "
            'request, e.g. SEC_USER_AGENT="Your Name you@example.com".'
        )
    set_identity(identity)

    filing = Company(ticker).get_filings(form="10-K").latest()
    return select_facts(filing.xbrl().facts.to_dataframe(), filing.accession_no)


def save_facts(facts: list[Fact], path: Path) -> None:
    path.write_bytes(FactList.dump_json(facts, indent=2) + b"\n")


def load_facts(path: Path) -> list[Fact]:
    return FactList.validate_json(path.read_bytes())
