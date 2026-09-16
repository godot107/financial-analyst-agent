"""A share price, so the memo can reach valuation ratios.

Filings hold no share price, so P/E and market-to-book are out of reach from
EDGAR alone. Alpha Vantage supplies the price - and only the price. Their
normalized fundamentals are deliberately not used: they carry no accession
number, so a figure taken from them could not be traced back to a filing, which
is the guarantee this whole project rests on.

The share count comes from the filing, so a market capitalisation is the
filing's shares times the market's price, and each half says where it came from.

No LLM here.
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import date

from pydantic import BaseModel, ConfigDict

from fin_analyst.edgar import Fact

ENDPOINT = "https://www.alphavantage.co/query"
TIMEOUT = 20


class Quote(BaseModel):
    """One share price, at a moment, from somewhere."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    price: float
    as_of: str  # the trading day the price belongs to
    source: str


class MarketDataUnavailable(RuntimeError):
    """No price, so no valuation ratios. Everything else still works."""


def fetch_quote(ticker: str, api_key: str | None = None, opener=urllib.request.urlopen) -> Quote:
    """The latest daily close, from Alpha Vantage's free GLOBAL_QUOTE endpoint."""
    api_key = api_key or os.environ.get("ALPHAVANTAGE_KEY")
    if not api_key:
        raise MarketDataUnavailable(
            "ALPHAVANTAGE_KEY is not set. A free key from alphavantage.co enables the "
            "valuation ratios; without it the memo simply has no price."
        )

    query = urllib.parse.urlencode(
        {"function": "GLOBAL_QUOTE", "symbol": ticker.upper(), "apikey": api_key}
    )
    with opener(f"{ENDPOINT}?{query}", timeout=TIMEOUT) as response:
        payload = json.loads(response.read())

    # Alpha Vantage answers 200 with a "Note" or "Information" when the free
    # allowance is spent, so an empty quote is the usual failure, not an error.
    quote = payload.get("Global Quote") or {}
    price = quote.get("05. price")
    if not price:
        reason = payload.get("Note") or payload.get("Information") or payload.get("Error Message")
        raise MarketDataUnavailable(
            f"no price for {ticker.upper()}: {reason or 'the response carried no quote'}"
        )

    return Quote(
        ticker=ticker.upper(),
        price=float(price),
        as_of=quote.get("07. latest trading day") or date.today().isoformat(),
        source="Alpha Vantage GLOBAL_QUOTE",
    )


def price_fact(quote: Quote, fiscal_year: int) -> Fact:
    """The price as a fact, so the ratio functions treat it like any other input.

    It is pinned to a fiscal year to sit beside that year's earnings, which is
    what a trailing P/E does - and why the memo must show the "as of" date: the
    price is today's, the earnings are the year's.
    """
    return Fact(
        line_item="share_price",
        fiscal_year=fiscal_year,
        value=quote.price,
        concept=quote.source,
        period=quote.as_of,
        accession=f"{quote.source}, {quote.as_of}",
    )
