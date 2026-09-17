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
    # Set when a later 10-K changed a figure an earlier one reported: this fact
    # is the later figure, and these say what was first filed and where.
    earlier_value: float | None = None
    earlier_accession: str | None = None


FactList = TypeAdapter(list[Fact])

# Part of the cache key for extracted facts. A filing never changes, but what we
# extract from it does: bump this when line items are added or tags change, or a
# cached filing keeps answering without the new lines.
EXTRACTION_VERSION = "v2"  # v2: operating income, interest expense, leases


class Filing(BaseModel):
    """Which filing the facts came from, for a caller that wants more than an accession number.

    Everything here comes from the EDGAR filing index, which is fetched anyway to
    find the latest 10-K, so describing a filing downloads nothing extra.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    company: str  # as registered with the SEC, e.g. "MICROSOFT CORP"
    cik: int
    # Standard Industrial Classification, e.g. 7372 software, 6021 banks.
    sic: int | None = None
    form: str  # "10-K"
    accession: str
    filed: str  # the date the SEC accepted it
    period: str | None = None  # the balance sheet date, added from the facts
    url: str  # the filing's index page on sec.gov


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
    # EBIT for interest coverage. Banks and insurers don't report it: their
    # income statement has no operating/non-operating split.
    "operating_income": ["us-gaap:OperatingIncomeLoss"],
    # Not optional: a company that doesn't report it (Apple nets it into other
    # income) gets no interest coverage, rather than an infinite one.
    # InterestExpenseOperating is left out on purpose - that is a bank's cost of
    # funds, not a borrower's interest bill.
    "interest_expense": [
        "us-gaap:InterestExpense",
        "us-gaap:InterestExpenseNonoperating",
        "us-gaap:InterestExpenseDebt",
    ],
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

# Lease liabilities, for the debt variant that counts them (Subramanyam Ch. 3:
# leases are financing, whatever the balance sheet calls them). Companies tag
# them two ways: Microsoft reports only the total, Costco and Apple the current
# and noncurrent parts, so each year takes the total if there is one and
# otherwise adds the parts.
LEASE_ITEMS = {
    "operating_lease_liabilities": (
        "us-gaap:OperatingLeaseLiability",
        ("us-gaap:OperatingLeaseLiabilityCurrent", "us-gaap:OperatingLeaseLiabilityNoncurrent"),
    ),
    "finance_lease_liabilities": (
        "us-gaap:FinanceLeaseLiability",
        ("us-gaap:FinanceLeaseLiabilityCurrent", "us-gaap:FinanceLeaseLiabilityNoncurrent"),
    ),
}

# Lines a company may simply not have. Absent means zero, not unknown.
OPTIONAL_ITEMS = {
    "short_term_investments",
    "accounts_receivable",
    "short_term_borrowings",
    "current_long_term_debt",
    "long_term_debt",
    "operating_lease_liabilities",
    "finance_lease_liabilities",
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

    facts += _lease_facts(instants, facts, accession)
    return _fill_optional(facts, instants, accession)


def _lease_facts(instants: pd.DataFrame, facts: list[Fact], accession: str) -> list[Fact]:
    """Lease liabilities per balance sheet date: the total, or the two parts added."""
    # Some companies put finance leases inside a debt line already
    # ("LongTermDebtAndCapitalLeaseObligations"). Adding them again would count
    # them twice, so for those years they are recorded as 0 with the reason.
    in_debt = {
        f.fiscal_year: f
        for f in facts
        if "CapitalLease" in f.concept or "FinanceLease" in f.concept
    }
    leases = [
        Fact(line_item="finance_lease_liabilities", fiscal_year=year, value=0.0,
             concept=f"included in {debt.concept}", period=debt.period, accession=accession)
        for year, debt in sorted(in_debt.items())
    ]
    for line_item, (total_tag, parts) in LEASE_ITEMS.items():
        by_date: dict[str, dict[str, float]] = {}
        for _, row in instants[instants["concept"].isin([total_tag, *parts])].iterrows():
            by_date.setdefault(str(row["period_instant"]), {})[str(row["concept"])] = float(
                row["numeric_value"]
            )
        for period, found in sorted(by_date.items()):
            year = fiscal_year_of(period)
            if line_item == "finance_lease_liabilities" and year in in_debt:
                continue  # recorded above as included in debt
            if total_tag in found:
                value, concept = found[total_tag], total_tag
            elif all(part in found for part in parts):
                value, concept = sum(found[part] for part in parts), " + ".join(parts)
            else:
                continue  # only one part: better unknown than half a liability
            leases.append(
                Fact(line_item=line_item, fiscal_year=year, value=value, concept=concept,
                     period=period, accession=accession)
            )
    return leases


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


def identify(identity: str | None = None) -> None:
    """Every SEC request must carry a name and email (their fair-access rules)."""
    identity = identity or os.environ.get("SEC_USER_AGENT")
    if not identity:
        raise RuntimeError(
            "SEC_USER_AGENT is not set. The SEC requires a name and email on every "
            'request, e.g. SEC_USER_AGENT="Your Name you@example.com".'
        )
    set_identity(identity)


def latest_10k(ticker: str, identity: str | None = None, company=Company):
    """The company's most recent 10-K. A cheap lookup: metadata, not the filing."""
    identify(identity)
    return company(ticker).get_filings(form="10-K").latest()


def describe_filing(ticker: str, identity: str | None = None, company=Company) -> Filing:
    """The latest 10-K's index entry: who filed it, when, and where to read it."""
    identify(identity)
    entity = company(ticker)
    filing = entity.get_filings(form="10-K").latest()
    sic = getattr(entity, "sic", None)
    return Filing(
        ticker=ticker.upper(),
        company=filing.company,
        cik=int(filing.cik),
        sic=int(sic) if sic else None,
        form=filing.form,
        accession=filing.accession_no,
        filed=str(filing.filing_date),
        # Built from the cik and accession, not `filing.period_of_report`,
        # which downloads the whole submission to read one date.
        url=f"https://www.sec.gov/Archives/edgar/data/{int(filing.cik)}/{filing.accession_no}-index.html",
    )


def latest_accession(ticker: str, identity: str | None = None, company=Company) -> str:
    return latest_10k(ticker, identity, company).accession_no


MAX_FILINGS = 5


def fetch_facts(
    ticker: str, identity: str | None = None, cache=None, company=Company, filings: int = 1
) -> list[Fact]:
    """Download the company's latest 10-K - or the latest few - and pull our line items out.

    One 10-K holds two balance sheets and three years of income. Each earlier
    filing adds a year to both. Where filings disagree about the same year, the
    later filing's figure is used and the first-filed one is kept beside it.

    With a cache, each filing's XBRL is downloaded once per accession number and
    never again: a filing does not change after it is accepted. Which filings are
    the latest is still looked up every time, so a new 10-K is never missed.
    """
    if not 1 <= filings <= MAX_FILINGS:
        raise ValueError(f"filings must be between 1 and {MAX_FILINGS}, not {filings}")
    identify(identity)
    found = company(ticker).get_filings(form="10-K")
    # latest(1) returns one filing; latest(n) returns a list-like of them.
    recent = [found.latest()] if filings == 1 else list(found.latest(filings))
    return merge_filings([_facts_of(filing, cache) for filing in recent])


def _facts_of(filing, cache) -> list[Fact]:
    key = f"filings/{filing.accession_no}/facts-{EXTRACTION_VERSION}.json"
    if cache is not None and (hit := cache.get(key)) is not None:
        return FactList.validate_json(hit)

    facts = select_facts(filing.xbrl().facts.to_dataframe(), filing.accession_no)
    if cache is not None and facts:
        cache.put(key, FactList.dump_json(facts))
    return facts


def merge_filings(per_filing: list[list[Fact]]) -> list[Fact]:
    """One fact per line item and year, from 10-Ks given newest first.

    The newest filing that reports a figure wins: a later 10-K's comparative
    column carries any restatement. A zero filled in for an unreported line
    never beats a figure some filing actually reported. When an earlier filing
    reported something different, the fact records that earlier figure too -
    a restatement, or a change in which tag we matched, and either is worth
    seeing.
    """
    if len(per_filing) == 1:
        return per_filing[0]

    chosen: dict[tuple[str, int], Fact] = {}
    for facts in per_filing:  # newest first
        for fact in facts:
            key = (fact.line_item, fact.fiscal_year)
            if key not in chosen or (fact.reported and not chosen[key].reported):
                chosen[key] = fact

    merged = []
    for key, fact in chosen.items():
        # The oldest filing that reported this year: what was first filed.
        earlier = [
            f for facts in reversed(per_filing) for f in facts
            if (f.line_item, f.fiscal_year) == key and f.reported
            and f.accession != fact.accession
        ]
        if fact.reported and earlier and abs(earlier[0].value - fact.value) > 0.5:
            fact = fact.model_copy(
                update={"earlier_value": earlier[0].value, "earlier_accession": earlier[0].accession}
            )
        merged.append(fact)
    return sorted(merged, key=lambda f: (f.line_item, -f.fiscal_year))


def save_facts(facts: list[Fact], path: Path) -> None:
    path.write_bytes(FactList.dump_json(facts, indent=2) + b"\n")


def load_facts(path: Path) -> list[Fact]:
    return FactList.validate_json(path.read_bytes())
