"""A share price, and the ratios that need one. No network: the opener is faked."""

import io
import json

import pytest

from fin_analyst.edgar import Fact
from fin_analyst.market import MarketDataUnavailable, fetch_quote, price_fact
from fin_analyst.metrics import MARKET_METRIC_IDS, compute_all

QUOTE = {
    "Global Quote": {
        "01. symbol": "MSFT",
        "05. price": "512.4000",
        "07. latest trading day": "2026-09-15",
    }
}


def opener_returning(payload):
    def opener(url, timeout=None):
        opener.url = url
        return io.BytesIO(json.dumps(payload).encode())

    return opener


def fact(line_item, value, year=2026):
    return Fact(
        line_item=line_item,
        fiscal_year=year,
        value=float(value),
        concept="us-gaap:Test",
        period="2026-06-30",
        accession="acc",
    )


# --- fetching -------------------------------------------------------------


def test_a_quote_carries_its_price_date_and_source():
    quote = fetch_quote("msft", api_key="demo", opener=opener_returning(QUOTE))

    assert quote.ticker == "MSFT" and quote.price == pytest.approx(512.4)
    assert quote.as_of == "2026-09-15"
    assert "Alpha Vantage" in quote.source


def test_the_key_is_required_and_the_message_says_what_it_buys(monkeypatch):
    monkeypatch.delenv("ALPHAVANTAGE_KEY", raising=False)
    with pytest.raises(MarketDataUnavailable, match="valuation ratios"):
        fetch_quote("MSFT", opener=opener_returning(QUOTE))


def test_a_spent_allowance_is_reported_not_mistaken_for_a_price():
    """Alpha Vantage answers 200 with a note when the free allowance runs out."""
    spent = {"Note": "Thank you for using Alpha Vantage! Our standard API rate limit is 25 per day"}
    with pytest.raises(MarketDataUnavailable, match="rate limit"):
        fetch_quote("MSFT", api_key="demo", opener=opener_returning(spent))


def test_an_unknown_ticker_gives_no_price():
    with pytest.raises(MarketDataUnavailable, match="no price for NOPE"):
        fetch_quote("nope", api_key="demo", opener=opener_returning({"Global Quote": {}}))


# --- the price as a fact --------------------------------------------------


def test_the_price_says_where_and_when_it_came_from():
    quote = fetch_quote("MSFT", api_key="demo", opener=opener_returning(QUOTE))
    priced = price_fact(quote, 2026)

    assert priced.line_item == "share_price" and priced.fiscal_year == 2026
    assert priced.period == "2026-09-15"
    assert "Alpha Vantage" in priced.accession and "2026-09-15" in priced.accession


# --- the valuation ratios -------------------------------------------------


def test_valuation_ratios_use_the_filing_s_share_count_and_the_market_s_price():
    facts = [
        fact("share_price", 500),
        fact("diluted_shares", 7_450_000_000),
        fact("net_income", 133_749_000_000),
        fact("equity", 442_387_000_000),
        fact("revenue", 331_839_000_000),
        fact("cash", 20_935_000_000),
        fact("short_term_borrowings", 0),
        fact("current_long_term_debt", 9_227_000_000),
        fact("long_term_debt", 31_067_000_000),
    ]
    results = {r.metric_id: r for r in compute_all(facts, sorted(MARKET_METRIC_IDS))}
    market_cap = 500 * 7_450_000_000

    assert results["pe_ratio"].value == pytest.approx(market_cap / 133_749_000_000)
    assert results["market_to_book"].value == pytest.approx(market_cap / 442_387_000_000)
    enterprise_value = market_cap + 9_227_000_000 + 31_067_000_000 - 20_935_000_000
    assert results["ev_to_revenue"].value == pytest.approx(enterprise_value / 331_839_000_000)


def test_without_a_price_the_valuation_ratios_say_so_rather_than_guessing():
    facts = [fact("net_income", 100), fact("equity", 500), fact("diluted_shares", 10)]
    results = {r.metric_id: r for r in compute_all(facts, ["pe_ratio", "market_to_book"])}

    assert results["pe_ratio"].value is None
    assert "share_price" in results["pe_ratio"].reason


def test_a_loss_making_company_gets_no_pe():
    facts = [fact("share_price", 10), fact("diluted_shares", 100), fact("net_income", -50)]
    result = compute_all(facts, ["pe_ratio"])[0]

    assert result.value is None
    assert "negative" in result.reason
