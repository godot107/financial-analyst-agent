"""Finding the paragraph that answers the question."""

from pathlib import Path

import pytest

from fin_analyst.passages import (
    MIN_CHARS, Passage, load_passages, name_words, search, split_passages, stem,
)

FIXTURE = Path(__file__).parent / "fixtures" / "msft_passages.json"


@pytest.fixture(scope="module")
def msft():
    return load_passages(FIXTURE)


def passage(id, text):
    return Passage(id=id, item="Item 7", text=text, ticker="MSFT", accession="acc")


# --- splitting ------------------------------------------------------------


def test_paragraphs_become_passages_with_running_ids():
    text = "A" * 300 + "\n\n" + "B" * 300
    passages = split_passages(text, "Item 7", "MSFT", "acc")
    assert [p.id for p in passages] == ["P1", "P2"]
    assert all(p.item == "Item 7" and p.accession == "acc" for p in passages)


def test_headings_and_stray_lines_are_dropped():
    """A citation should point at an argument, not at a heading."""
    text = "ITEM 7. MANAGEMENT'S DISCUSSION\n\n" + "A" * (MIN_CHARS + 10)
    assert len(split_passages(text, "Item 7", "MSFT", "acc")) == 1


def test_a_very_long_paragraph_is_cut_down():
    passages = split_passages("A" * 5000, "Item 7", "MSFT", "acc")
    assert 0 < len(passages[0].text) <= 1400


# --- matching words -------------------------------------------------------


@pytest.mark.parametrize(
    "a, b",
    [("increase", "increased"), ("expense", "expenses"), ("operating", "operate"), ("margin", "margins")],
)
def test_words_match_across_their_endings(a, b):
    assert stem(a) == stem(b)


def test_the_explanation_outranks_a_paragraph_that_merely_repeats_the_words(msft):
    """BM25 alone put the currency paragraph first: it repeats "expenses", while
    "increase" never matched "increased". That is the bug this guards."""
    top = search(msft, "Why did operating expenses increase?", 3)
    assert top[0].text.startswith("Operating expenses increased")
    assert "foreign exchange" not in top[0].text


def test_a_phrase_beats_the_same_words_scattered():
    passages = [
        passage("P1", "Operating expenses increased on research investment. " * 4),
        passage("P2", "Our operations span many regions and our expenses vary with them. " * 4),
    ]
    assert search(passages, "Why did operating expenses increase?", 1)[0].id == "P1"


def test_a_passage_sharing_no_words_with_the_question_is_not_returned():
    """A citation to it would be decoration, not evidence."""
    passages = [passage("P1", "The board declared a quarterly dividend. " * 8)]
    assert search(passages, "What drove gross margin?", 4) == []


def test_nothing_to_search_is_not_an_error():
    assert search([], "anything?") == []


# --- the company's own name ------------------------------------------------


def test_liquid_finds_liquidity_and_liabilities_find_a_liability():
    assert stem("liquid") == stem("liquidity")
    assert stem("liabilities") == stem("liability")


def test_the_company_name_is_left_out_of_the_search(msft):
    """The first live memo on Lambda: "How liquid is Microsoft?" retrieved three
    Microsoft 365 revenue paragraphs, and the writer cited two to dismiss them."""
    top = search(msft, "How liquid is Microsoft?", 4, name_words("MICROSOFT CORP"))
    assert top[0].text.startswith("Cash, cash equivalents, and short-term investments")
    assert not any("Microsoft 365" in p.text[:40] for p in top)


def test_name_words_drop_legal_words_and_never_the_ticker():
    """COST is Costco's ticker, and "costs" is a word a question needs."""
    assert name_words("COSTCO WHOLESALE CORP /NEW/") == {"costco", "wholesal"}
    assert name_words(None) == set()
