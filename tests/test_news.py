"""What has happened since the filing: press releases, and an optional index."""

import io
import json

import pytest

from fin_analyst.memo import build_footer, build_sources, find_problems
from fin_analyst.news import GDELT_CREDIT, fetch_gdelt, news_credit
from fin_analyst.passages import Passage

ARTICLES = {
    "articles": [
        {
            "title": "Microsoft lifts cloud capacity guidance",
            "domain": "reuters.com",
            "url": "https://reuters.com/a",
            "seendate": "20260910T120000Z",
        },
        {"title": "", "domain": "cnbc.com", "url": "https://cnbc.com/b", "seendate": "20260909T0"},
    ]
}


def opener_returning(payload):
    def opener(url, timeout=None):
        opener.url = url
        return io.BytesIO(json.dumps(payload).encode())

    return opener


def news_passage(id="N1", text="Microsoft lifts cloud capacity guidance"):
    return Passage(
        id=id,
        item="News, reuters.com, 2026-09-10",
        text=text,
        ticker="MSFT",
        accession="GDELT",
        url="https://reuters.com/a",
        published="2026-09-10",
    )


# --- the index ------------------------------------------------------------


def test_headlines_carry_their_source_date_and_link():
    opener = opener_returning(ARTICLES)
    passages = fetch_gdelt("Microsoft", opener=opener)

    assert len(passages) == 1  # the untitled one is dropped
    article = passages[0]
    assert article.id == "N1" and article.published == "2026-09-10"
    assert article.url == "https://reuters.com/a"
    assert "reuters.com" in article.item


def test_the_query_is_restricted_to_business_desks():
    """Unfiltered, a company name returns market commentary and stock promotion."""
    opener = opener_returning(ARTICLES)
    fetch_gdelt("Microsoft", opener=opener)
    assert "reuters.com" in opener.url and "cnbc.com" in opener.url


def test_an_unreachable_index_does_not_stop_a_filing_based_memo():
    def broken(url, timeout=None):
        raise OSError("network down")

    assert fetch_gdelt("Microsoft", opener=broken) == []


# --- how news appears in the memo ----------------------------------------


def test_a_news_citation_is_allowed_and_checked_like_any_other():
    passages = [news_passage()]
    assert find_problems("Capacity guidance rose [N1].", [], (), passages) == []
    assert find_problems("Capacity guidance rose [N7].", [], (), passages)


def test_the_source_list_links_news_instead_of_naming_a_filing():
    text = build_sources("Guidance rose [N1].", [news_passage()])
    assert "https://reuters.com/a" in text
    assert "News, reuters.com, 2026-09-10" in text


def test_gdelt_is_credited_only_when_its_data_is_used():
    """Its terms allow any use and ask for a citation, so the memo carries one."""
    assert news_credit([news_passage()]) == GDELT_CREDIT
    filing_only = Passage(id="P1", item="Item 7", text="t", ticker="MSFT", accession="acc")
    assert news_credit([filing_only]) is None

    footer = build_footer([], [], "MSFT", passages=[news_passage()])
    assert "gdeltproject.org" in footer


# --- dates are the one number news may carry ------------------------------


def test_a_date_the_source_carries_is_not_a_leaked_number():
    """A memo saying what was announced without saying when is worse than useless."""
    passages = [news_passage()]
    for written in (
        "Announced on 2026-09-10 [N1].",
        "Announced on September 10, 2026 [N1].",
        "Announced on 10 September 2026 [N1].",
    ):
        assert find_problems(written, [], (), passages) == [], written


def test_a_date_no_source_carries_is_still_caught():
    assert find_problems("Announced on 2025-01-04 [N1].", [], (), [news_passage()])


def test_a_figure_next_to_a_date_is_still_caught():
    problems = find_problems(
        "On 2026-09-10 it guided to $5.2 billion [N1].", [], (), [news_passage()]
    )
    assert problems and "wrote yourself" in problems[0]
