"""The chat agent's tools, without a model: what they return and what they remember."""

import pytest

from fin_analyst.chat_tools import ChatToolbox
from fin_analyst.edgar import load_facts
from fin_analyst.passages import Passage, load_passages
from tests.test_graph import FIXTURE, PASSAGES_FIXTURE


@pytest.fixture
def facts():
    return load_facts(FIXTURE)


@pytest.fixture
def toolbox(facts):
    return ChatToolbox("msft", facts, load_passages(PASSAGES_FIXTURE), company="MICROSOFT CORP")


def test_get_metrics_returns_placeholders_and_remembers_them(toolbox):
    shown = toolbox.get_metrics(["current_ratio"])
    assert "{{current_ratio:2026}}" in shown
    assert ("current_ratio", 2026) in toolbox.shown_metrics


def test_unknown_ids_are_refused_not_computed(toolbox):
    # The schema isn't strict, so a made-up id can arrive; it must not become a metric.
    shown = toolbox.get_metrics(["current_ratio", "liquidity_vibes"])
    assert "Not metric ids: ['liquidity_vibes']" in shown
    assert {key[0] for key in toolbox.shown_metrics} == {"current_ratio"}


def test_an_answer_may_use_only_placeholders_it_was_shown(toolbox):
    answer = "The current ratio {{current_ratio:2025->2026}}."
    assert toolbox.check(answer)  # nothing fetched yet
    toolbox.get_metrics(["current_ratio"])
    assert toolbox.check(answer) == []
    assert "{{" not in toolbox.render(answer)


def test_a_leaked_digit_is_still_caught(toolbox):
    toolbox.get_metrics(["current_ratio"])
    problems = toolbox.check("The current ratio fell to 1.23x.")
    assert any("number you wrote yourself" in p for p in problems)


def test_an_answer_may_cite_only_passages_it_read(toolbox):
    toolbox.get_metrics(["current_ratio"])
    answer = "The current ratio {{current_ratio:2025->2026}}, as cash was used for buybacks [P50]."
    assert any("[P50] is not a passage" in p for p in toolbox.check(answer))
    assert "[P50]" in toolbox.search_filing("liquidity cash")
    assert toolbox.check(answer) == []


def test_searching_without_the_filing_text_says_so(facts):
    toolbox = ChatToolbox("msft", facts)
    assert "not loaded" in toolbox.search_filing("liquidity")


def test_one_peer_per_conversation_fetched_once(facts):
    fetched = []

    def fetch_peer(ticker):
        fetched.append(ticker)
        return facts

    toolbox = ChatToolbox("msft", facts, fetch_peer=fetch_peer)
    assert "{{peer.current_ratio:2026}}" in toolbox.compare_with("googl", ["current_ratio"])
    toolbox.compare_with("GOOGL", ["quick_ratio"])
    assert fetched == ["GOOGL"]
    assert "allows one peer" in toolbox.compare_with("AAPL", ["current_ratio"])
    assert toolbox.check("Its current ratio {{peer.current_ratio:2025->2026}}.") == []


def test_no_peer_fetcher_means_no_comparison(toolbox):
    assert "not available" in toolbox.compare_with("GOOGL", ["current_ratio"])


def test_a_failed_fetch_is_a_message_not_a_crash(facts):
    def broken(ticker):
        raise RuntimeError("SEC is down")

    toolbox = ChatToolbox("msft", facts, fetch_peer=broken, fetch_news=broken)
    assert "SEC is down" in toolbox.compare_with("GOOGL", ["current_ratio"])
    assert toolbox.peer_ticker is None  # a later call may try again
    assert "SEC is down" in toolbox.recent_news("acquisition")


def test_news_is_fetched_once_and_cited_by_its_own_ids(facts):
    calls = []
    release = Passage(
        id="N1", item="8-K press release, 2026-09-30", text="Microsoft completed the acquisition "
        "of a cloud security company, adding to its security business.", ticker="MSFT",
        accession="0000000000-26-000001",
    )

    def fetch_news(ticker):
        calls.append(ticker)
        return [release]

    toolbox = ChatToolbox("msft", facts, fetch_news=fetch_news)
    assert "[N1]" in toolbox.recent_news("acquisition security")
    toolbox.recent_news("cloud")
    assert calls == ["MSFT"]
    assert "N1" in toolbox.shown_passages
