"""Iteration 2: the filing's narrative, so the memo can say *why*.

Ratios say what moved. Item 7 (MD&A) and Item 1A (risk factors) say what
management thinks about it. This module pulls those sections, splits them into
paragraphs, and finds the few that bear on a question.

Retrieval is BM25 - exact terms, cheap, no model and no vector store. Huyen Ch. 6
notes term-based retrieval "works well out of the box", and financial questions
lean on exact words ("operating expenses", "ASC 842") that embeddings blur.
Reach for embeddings only if this falls short.

No LLM here: the writer receives passages, but nothing in this file calls a model.
"""

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter
from rank_bm25 import BM25Okapi

# Long enough to carry an idea, short enough that a citation points somewhere
# specific. Below the floor is usually a heading or a stray table row.
MIN_CHARS = 200
MAX_CHARS = 1400


class Passage(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str  # what the writer cites, e.g. "P3", or "N1" for news
    item: str  # "Item 7", "Item 1A", or a dated label for news
    text: str
    ticker: str
    accession: str  # the filing, or the source for news
    url: str | None = None  # news only: where a reader can check it
    published: str | None = None  # news only: the date it carries


PassageList = TypeAdapter(list[Passage])


def split_passages(text: str, item: str, ticker: str, accession: str, start: int = 1) -> list[Passage]:
    """Paragraphs, with the runts dropped and the giants cut down."""
    passages = []
    for block in re.split(r"\n\s*\n", text or ""):
        block = " ".join(block.split())
        if len(block) < MIN_CHARS:
            continue
        passages.append(
            Passage(
                id=f"P{start + len(passages)}",
                item=item,
                text=block[:MAX_CHARS],
                ticker=ticker,
                accession=accession,
            )
        )
    return passages


def fetch_passages(
    ticker: str, identity: str | None = None, cache=None, company=None
) -> list[Passage]:
    """MD&A and risk factors from the company's latest 10-K, cached by accession number."""
    from fin_analyst.edgar import Company, latest_10k

    filing = latest_10k(ticker, identity, company or Company)
    key = f"filings/{filing.accession_no}/passages.json"
    if cache is not None and (hit := cache.get(key)) is not None:
        return PassageList.validate_json(hit)

    report = filing.obj()
    passages: list[Passage] = []
    for item, text in (
        ("Item 7", report.management_discussion),
        ("Item 1A", report.risk_factors),
    ):
        passages += split_passages(
            str(text or ""), item, ticker.upper(), filing.accession_no, start=len(passages) + 1
        )
    if cache is not None and passages:
        cache.put(key, PassageList.dump_json(passages))
    return passages


# Question words carry no signal and crowd the scoring.
STOPWORDS = {"why", "did", "does", "do", "what", "how", "is", "are", "the", "a", "an", "of",
             "in", "on", "for", "to", "and", "or", "its", "it", "this", "that", "was", "were"}
# How much a matched phrase lifts a passage, as a fraction of its own score.
PHRASE_WEIGHT = 1.0


def stem(word: str) -> str:
    """Enough stemming to match "increase" with "increased".

    Without it, a question asking why expenses *increase* misses the paragraph
    saying they *increased*, and a paragraph that merely repeats "expenses"
    outranks the one that explains them.
    """
    word = word.removesuffix("'s")
    # "-ity" and "-ities" too, so "liquid" finds "liquidity" and "liabilities"
    # finds "liability". Without it, "How liquid is Microsoft?" matched only
    # one liquidity paragraph and filled the rest with Microsoft 365 revenue.
    for suffix, keep in (
        ("ities", 7), ("ity", 6), ("ing", 5), ("ed", 4), ("es", 4), ("s", 3), ("e", 4)
    ):
        if word.endswith(suffix) and len(word) > keep:
            return word[: -len(suffix)]
    return word


def tokenize(text: str, drop_stopwords: bool = False) -> list[str]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    if drop_stopwords:
        words = [w for w in words if w not in STOPWORDS]
    return [stem(w) for w in words]


def _phrase_bonus(query_tokens: list[str], passage_tokens: list[str]) -> float:
    """The share of the question's word pairs that appear in the passage.

    "operating expenses" as a phrase means far more than the two words apart,
    and finance questions are full of such pairs: gross margin, deferred revenue.
    """
    pairs = list(zip(query_tokens, query_tokens[1:]))
    if not pairs:
        return 0.0
    passage_pairs = set(zip(passage_tokens, passage_tokens[1:]))
    return sum(pair in passage_pairs for pair in pairs) / len(pairs)


# Words in a registered name that say what kind of entity it is, not which one.
LEGAL_WORDS = {"corp", "corporation", "inc", "incorporated", "co", "company", "ltd", "limited",
               "plc", "llc", "lp", "sa", "nv", "ag", "holding", "group", "the", "new", "de"}


def name_words(company: str | None) -> set[str]:
    """The words that name the company, to leave out of a search of its own filing.

    Every question names the company and so does much of the filing, so the name
    matches paragraphs for no reason: "How liquid is Microsoft?" retrieved three
    paragraphs about Microsoft 365 revenue. The ticker is not included: tickers
    are often ordinary words (COST, NOW), and "costs" matters in a question.
    """
    return set(tokenize(company or "")) - {stem(w) for w in LEGAL_WORDS}


def search(
    passages: list[Passage], query: str, k: int = 4, ignore: set[str] = frozenset()
) -> list[Passage]:
    """The k passages that best match the question, best first.

    `ignore` holds words to leave out of the question, such as the company's name.
    """
    if not passages:
        return []
    documents = [tokenize(p.text) for p in passages]
    index = BM25Okapi(documents)
    query_tokens = [t for t in tokenize(query, drop_stopwords=True) if t not in ignore]
    if not query_tokens:
        return []

    scores = index.get_scores(query_tokens)
    wanted = set(query_tokens)

    # Sharing no words with the question is the real exclusion rule. Don't test
    # the score for that: with few passages BM25 gives common terms a negative
    # weight, and a sign test then throws away every result.
    candidates = [
        (_phrase_bonus(query_tokens, document), score, passage)
        for document, score, passage in zip(documents, scores, passages)
        if wanted & set(document)
    ]
    # A passage containing the question's own phrase ("operating expenses")
    # ranks above one that merely scatters those words; BM25 breaks the ties.
    candidates.sort(key=lambda row: (-row[0], -row[1]))
    return [passage for _, _, passage in candidates[:k]]


def save_passages(passages: list[Passage], path: Path) -> None:
    path.write_bytes(PassageList.dump_json(passages, indent=2) + b"\n")


def load_passages(path: Path) -> list[Passage]:
    return PassageList.validate_json(path.read_bytes())
