"""Follow-up questions: one fetch, shared budget, a hard turn cap."""

import pytest

from fin_analyst.config import CHAT_TURN_CEILING, load_settings
from fin_analyst.chat import ChatSession
from fin_analyst.edgar import load_facts
from tests.test_graph import CLEAN_DRAFT, FIXTURE, FakeAnalyst


@pytest.fixture
def facts():
    loaded = load_facts(FIXTURE)
    return loaded


@pytest.fixture
def settings():
    return load_settings()


def session(settings, facts, tmp_path, drafts, cost=0.01):
    counter = {"fetches": 0}

    def fetch(ticker):
        counter["fetches"] += 1
        return facts

    analyst = FakeAnalyst(drafts, cost=cost)
    chat = ChatSession("msft", analyst, settings, fetch=fetch, runs_dir=tmp_path)
    return chat, analyst, counter


def test_the_filing_is_fetched_once_however_many_questions(settings, facts, tmp_path):
    chat, _, counter = session(settings, facts, tmp_path, [CLEAN_DRAFT] * 3)
    for _ in range(3):
        chat.ask("How liquid is it?")
    assert counter["fetches"] == 1


def test_later_turns_see_the_earlier_questions_and_answers(settings, facts, tmp_path):
    chat, analyst, _ = session(settings, facts, tmp_path, [CLEAN_DRAFT] * 2)
    chat.ask("How liquid is it?")
    chat.ask("And what about leverage?")

    assert analyst.history_seen[0] == []  # first turn has nothing to build on
    asked, answered = analyst.history_seen[1][0]
    assert asked == "How liquid is it?"
    assert "1.23x" in answered  # the rendered answer, not the placeholder draft


def test_the_budget_is_shared_across_the_conversation(settings, facts, tmp_path):
    """Otherwise each turn would get a fresh $1 and a long chat could run away."""
    # Two calls per turn (plan + write), so 5 calls fit inside the budget and
    # the sixth does not.
    per_call = settings.max_usd_per_run / 5
    chat, _, _ = session(settings, facts, tmp_path, [CLEAN_DRAFT] * 4, cost=per_call)

    states = [chat.ask(f"question {i}?") for i in range(3)]
    assert states[0].memo and states[1].memo
    assert states[-1].memo is None
    assert "over the" in states[-1].error


def test_the_turn_cap_is_enforced(settings, facts, tmp_path):
    chat, _, _ = session(settings, facts, tmp_path, [CLEAN_DRAFT] * 10)
    for i in range(settings.chat_max_turns):
        assert chat.turns_left == settings.chat_max_turns - i
        chat.ask(f"question {i}?")

    assert chat.turns_left == 0
    with pytest.raises(RuntimeError, match="used its"):
        chat.ask("one more?")


def test_the_cap_cannot_be_configured_above_the_ceiling(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "nodes:\n  plan: {model: claude-opus-5, effort: low, max_tokens: 512}\n"
        "prices_per_million_tokens:\n  claude-opus-5: {input: 5, output: 25}\n"
        "max_usd_per_run: 1.0\nmax_retries: 2\n"
        f"chat_max_turns: {CHAT_TURN_CEILING + 1}\n"
    )
    with pytest.raises(ValueError, match="chat_max_turns"):
        load_settings(config)


def test_a_rejected_answer_does_not_become_context(settings, facts, tmp_path):
    """A memo that failed the check is not something later turns should build on."""
    leaky = "Liquidity is fine: the ratio is 1.23."
    chat, _, _ = session(settings, facts, tmp_path, [leaky] * 3 + [CLEAN_DRAFT])
    failed = chat.ask("How liquid is it?")

    assert failed.memo is None
    assert chat.history == []
    assert chat.turns_left == settings.chat_max_turns


def test_a_peer_is_fetched_once_for_the_whole_conversation(settings, facts, tmp_path):
    fetched = []

    def fetch(ticker):
        fetched.append(ticker)
        return facts

    analyst = FakeAnalyst([CLEAN_DRAFT] * 2)
    chat = ChatSession(
        "msft", analyst, settings, fetch=fetch, runs_dir=tmp_path, peer_ticker="googl"
    )
    chat.ask("How do they compare on liquidity?")
    chat.ask("And on leverage?")

    assert fetched == ["MSFT", "GOOGL"]  # two filings, not four
    assert [seen[0] for seen in analyst.peers_seen] == ["GOOGL", "GOOGL"]
