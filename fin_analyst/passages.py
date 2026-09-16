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

import os
import re
from pathlib import Path

from edgar import Company, set_identity
from pydantic import BaseModel, ConfigDict, TypeAdapter
from rank_bm25 import BM25Okapi

# Long enough to carry an idea, short enough that a citation points somewhere
# specific. Below the floor is usually a heading or a stray table row.
MIN_CHARS = 200
MAX_CHARS = 1400


class Passage(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str  # what the writer cites, e.g. "P3"
    item: str  # "Item 7" or "Item 1A"
    text: str
    ticker: str
    accession: str


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


def fetch_passages(ticker: str, identity: str | None = None) -> list[Passage]:
    """MD&A and risk factors from the company's latest 10-K."""
    identity = identity or os.environ.get("SEC_USER_AGENT")
    if not identity:
        raise RuntimeError("SEC_USER_AGENT is not set; the SEC requires a name and email")
    set_identity(identity)

    filing = Company(ticker).get_filings(form="10-K").latest()
    report = filing.obj()

    passages: list[Passage] = []
    for item, text in (
        ("Item 7", report.management_discussion),
        ("Item 1A", report.risk_factors),
    ):
        passages += split_passages(
            str(text or ""), item, ticker.upper(), filing.accession_no, start=len(passages) + 1
        )
    return passages


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def search(passages: list[Passage], query: str, k: int = 4) -> list[Passage]:
    """The k passages that best match the question, best first."""
    if not passages:
        return []
    index = BM25Okapi([tokenize(p.text) for p in passages])
    scores = index.get_scores(tokenize(query))
    ranked = sorted(zip(scores, passages), key=lambda pair: -pair[0])
    # A zero score means the question shares no terms with the passage; a
    # citation to it would be decoration, not evidence.
    return [passage for score, passage in ranked[:k] if score > 0]


def save_passages(passages: list[Passage], path: Path) -> None:
    path.write_bytes(PassageList.dump_json(passages, indent=2) + b"\n")


def load_passages(path: Path) -> list[Passage]:
    return PassageList.validate_json(path.read_bytes())
