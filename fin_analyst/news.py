"""What has happened since the filing.

A 10-K is up to a year old by the time anyone reads it. Two sources fill the gap,
both free and neither needing a key:

1. **8-K exhibits** - the company's own press releases, filed with the SEC, so
   each one carries an accession number like everything else here.
2. **GDELT** - a news index, for when a filing has nothing to say. Its terms
   allow "unlimited and unrestricted use for any academic, commercial, or
   governmental use", asking only that the project be cited, which the memo
   footer does. NewsAPI's free plan was the alternative and was rejected: it
   forbids staging or production use, "including internally".

News arrives as ordinary passages, so the citation, checking and rendering
already built all apply to it. Two rules matter especially here: it supplies
words and never numbers, and it is untrusted text - anything in it that looks
like an instruction is quoted material, not a command.
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import date, timedelta

from edgar import Company, set_identity

from fin_analyst.passages import MAX_CHARS, MIN_CHARS, Passage, split_passages

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
TIMEOUT = 20
GDELT_CREDIT = "News headlines via the GDELT Project (https://www.gdeltproject.org/)."
# An unfiltered index answers a question about Microsoft's margins with "3
# Unstoppable Dow Stocks Worth Buying Right Now". Restricting it to business
# desks costs recall and buys a corpus worth citing.
BUSINESS_DOMAINS = (
    "reuters.com",
    "cnbc.com",
    "ft.com",
    "wsj.com",
    "bloomberg.com",
    "marketwatch.com",
    "barrons.com",
    "seekingalpha.com",
)


def fetch_press_releases(
    ticker: str, limit: int = 2, identity: str | None = None, start: int = 1
) -> list[Passage]:
    """Paragraphs from the most recent 8-K press-release exhibits."""
    identity = identity or os.environ.get("SEC_USER_AGENT")
    if not identity:
        raise RuntimeError("SEC_USER_AGENT is not set; the SEC requires a name and email")
    set_identity(identity)

    passages: list[Passage] = []
    for filing in Company(ticker).get_filings(form="8-K").head(limit):
        exhibits = [
            a for a in filing.attachments if str(getattr(a, "document_type", "")).startswith("EX-99")
        ]
        for exhibit in exhibits[:1]:  # the press release itself, not the cover
            found = split_passages(
                exhibit.text(),
                f"8-K exhibit, filed {filing.filing_date}",
                ticker.upper(),
                filing.accession_no,
                start=start + len(passages),
            )
            for passage in found:
                passages.append(
                    passage.model_copy(
                        update={
                            "id": passage.id.replace("P", "N"),
                            "published": str(filing.filing_date),
                        }
                    )
                )
    return passages


def fetch_gdelt(
    company: str,
    days: int = 30,
    max_records: int = 8,
    start: int = 1,
    opener=urllib.request.urlopen,
    domains: tuple[str, ...] = BUSINESS_DOMAINS,
) -> list[Passage]:
    """Recent headlines about the company, from the GDELT index."""
    outlets = " OR ".join(f"domain:{d}" for d in domains)
    query = urllib.parse.urlencode(
        {
            "query": f'"{company}" ({outlets})' if domains else f'"{company}" sourcelang:english',
            "mode": "artlist",
            "format": "json",
            "maxrecords": max_records,
            "startdatetime": (date.today() - timedelta(days=days)).strftime("%Y%m%d") + "000000",
            "sort": "datedesc",
        }
    )
    try:
        with opener(f"{GDELT_ENDPOINT}?{query}", timeout=TIMEOUT) as response:
            payload = json.loads(response.read() or b"{}")
    except Exception:
        # A news index being unreachable must not stop a filing-based memo.
        return []

    passages = []
    for article in payload.get("articles", [])[:max_records]:
        title = " ".join(str(article.get("title", "")).split())
        if not title:
            continue
        seen = str(article.get("seendate", ""))[:8]
        published = f"{seen[:4]}-{seen[4:6]}-{seen[6:8]}" if len(seen) == 8 else None
        passages.append(
            Passage(
                id=f"N{start + len(passages)}",
                item=f"News, {article.get('domain', 'unknown source')}, {published or 'undated'}",
                text=title[:MAX_CHARS],
                ticker=company,
                accession="GDELT",
                url=article.get("url"),
                published=published,
            )
        )
    return passages


def fetch_news(
    ticker: str,
    company_name: str | None = None,
    identity: str | None = None,
    include_index: bool = False,
    gdelt_opener=urllib.request.urlopen,
) -> list[Passage]:
    """The company's own 8-K releases, and optionally a news index.

    The index is off by default, and that is a finding rather than a
    preference. Asked about Microsoft, even a business-desk-only query returns
    "Cramer reveals his favourite Mag 7 stock" and an upgrades-and-downgrades
    list: market commentary, not company facts, and nothing a memo should lean
    on. The 8-K exhibits are the company's own words and carry accession
    numbers, so they are the default.
    """
    releases = fetch_press_releases(ticker, identity=identity)
    if not include_index:
        return releases
    return releases + fetch_gdelt(
        company_name or ticker, start=len(releases) + 1, opener=gdelt_opener
    )


def news_credit(passages: list[Passage]) -> str | None:
    """GDELT asks to be cited wherever its data is used."""
    return GDELT_CREDIT if any(p.accession == "GDELT" for p in passages) else None
