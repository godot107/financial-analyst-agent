"""Step 4: the workflow, driven by a scripted stand-in for Claude.

No API key, no network: `fetch` is injected and the analyst is fake, so these
tests exercise the retry loop, the give-up path and the budget guard for real.
"""

import json
from pathlib import Path

import pytest

from fin_analyst.config import load_settings
from fin_analyst.edgar import load_facts
from fin_analyst.graph import AnalysisState, run_analysis
from fin_analyst.passages import load_passages, search

FIXTURE = Path(__file__).parent / "fixtures" / "msft_facts.json"
PASSAGES_FIXTURE = Path(__file__).parent / "fixtures" / "msft_passages.json"

CLEAN_DRAFT = "Liquidity eased: the current ratio {{current_ratio:2025->2026}}."
LEAKY_DRAFT = "Liquidity eased: the current ratio fell to 1.23, roughly 2x cover."


class FakeVerdict:
    def __init__(self, supported, reason="because the passage says so"):
        self.supported = supported
        self.reason = reason


class FakeAnalyst:
    """Returns scripted drafts and remembers what it was asked."""

    def __init__(self, drafts, metric_ids=("current_ratio",), cost=0.01, verdicts=None):
        self.drafts = list(drafts)
        self.metric_ids = list(metric_ids)
        self.cost = cost
        self.problems_seen = []
        self.history_seen = []
        self.peers_seen = []
        self.passages_seen = []
        # One list of verdicts per verify call; the default supports everything.
        self.verdicts = list(verdicts) if verdicts else None
        self.claims_seen = []

    def choose_metrics(self, ticker, question):
        return self.metric_ids, self.cost

    def verify_claims(self, claims):
        self.claims_seen.append(list(claims))
        if self.verdicts:
            return self.verdicts.pop(0), self.cost
        return [FakeVerdict(True) for _ in claims], self.cost

    def write_draft(
        self,
        ticker,
        question,
        metrics,
        problems,
        history=(),
        peer_ticker=None,
        peer_metrics=(),
        passages=(),
    ):
        self.problems_seen.append(problems)
        self.history_seen.append(list(history))
        self.peers_seen.append((peer_ticker, list(peer_metrics)))
        self.passages_seen.append(list(passages))
        return self.drafts.pop(0), self.cost


@pytest.fixture
def facts():
    """The same recorded filing for any ticker: these tests are about wiring."""
    loaded = load_facts(FIXTURE)
    return lambda ticker: loaded


@pytest.fixture
def settings():
    return load_settings()


def run(analyst, settings, facts, tmp_path, question="How liquid is Microsoft?", **kwargs):
    # fetch_text=None keeps the filing's narrative out of it; these tests are
    # about the workflow, and nothing here may touch the network.
    kwargs.setdefault("fetch_text", None)
    return run_analysis(
        "msft", question, analyst, settings, fetch=facts, runs_dir=tmp_path, **kwargs
    )


def records_in(tmp_path):
    return sorted(tmp_path.glob("*.json"))


# --- the three paths through the graph -----------------------------------


def test_a_clean_draft_becomes_a_memo(settings, facts, tmp_path):
    state = run(FakeAnalyst([CLEAN_DRAFT]), settings, facts, tmp_path)

    assert state.error is None
    assert "fell 0.12x to 1.23x" in state.memo  # the renderer wrote the direction
    assert "{{" not in state.memo
    assert "MSFT: SEC filing 0001193125-26-323660" in state.memo  # footer names the company
    assert "No peer comparison" in state.memo
    assert len(records_in(tmp_path)) == 1
    assert len(list(tmp_path.glob("*.md"))) == 1


def test_a_leaky_draft_is_retried_and_the_problems_are_sent_back(settings, facts, tmp_path):
    analyst = FakeAnalyst([LEAKY_DRAFT, CLEAN_DRAFT])
    state = run(analyst, settings, facts, tmp_path)

    assert state.error is None
    assert state.memo is not None
    assert len(state.drafts) == 2
    # The first attempt sees no problems; the retry is told exactly what was wrong.
    assert analyst.problems_seen[0] == []
    assert any("wrote yourself" in p for p in analyst.problems_seen[1])


def test_it_gives_up_rather_than_publishing_a_leaky_memo(settings, facts, tmp_path):
    state = run(FakeAnalyst([LEAKY_DRAFT] * 3), settings, facts, tmp_path)

    assert state.memo is None
    assert "gave up after 3 drafts" in state.error
    assert "wrote yourself" in state.error
    assert len(records_in(tmp_path)) == 1  # the failure is still recorded
    assert not list(tmp_path.glob("*.md"))


# --- the guards ----------------------------------------------------------


def test_a_plan_asking_for_a_metric_that_does_not_exist_is_an_error(settings, facts, tmp_path):
    analyst = FakeAnalyst([CLEAN_DRAFT], metric_ids=["ebitda_margin"])
    with pytest.raises(ValueError, match="do not exist"):
        run(analyst, settings, facts, tmp_path)


def test_a_run_that_costs_too_much_stops(settings, facts, tmp_path):
    expensive = FakeAnalyst([CLEAN_DRAFT], cost=settings.max_usd_per_run + 1)
    state = run(expensive, settings, facts, tmp_path)

    assert state.memo is None
    assert "over the" in state.error
    assert len(records_in(tmp_path)) == 1


# --- the run record ------------------------------------------------------


def test_the_record_says_what_happened(settings, facts, tmp_path):
    analyst = FakeAnalyst([LEAKY_DRAFT, CLEAN_DRAFT])
    state = run(analyst, settings, facts, tmp_path)

    record = json.loads(records_in(tmp_path)[0].read_text())
    assert record["ticker"] == "MSFT"
    assert record["question"] == "How liquid is Microsoft?"
    assert record["metric_ids"] == ["current_ratio"]
    assert len(record["drafts"]) == 2  # including the rejected one
    assert record["cost_usd"] == pytest.approx(0.03)  # plan + two writes
    assert record["memo"].startswith("Liquidity eased")
    # Facts are left out: they are large, and the accession number is in the footer.
    assert "facts" not in record


def test_state_starts_empty_but_valid():
    state = AnalysisState(ticker="MSFT", question="anything?")
    assert state.drafts == [] and state.cost_usd == 0.0 and state.memo is None


# --- comparing two companies --------------------------------------------

PEER_DRAFT = (
    "Liquidity eased: the current ratio {{current_ratio:2025->2026}}, "
    "against the peer's {{peer.current_ratio:2026}}."
)


def test_a_peer_is_fetched_computed_and_rendered(settings, facts, tmp_path):
    fetched = []

    def fetch(ticker):
        fetched.append(ticker)
        return facts(ticker)

    analyst = FakeAnalyst([PEER_DRAFT])
    state = run_analysis(
        "msft", "How does it compare?", analyst, settings,
        fetch=fetch, runs_dir=tmp_path, peer_ticker="googl", fetch_text=None,
    )

    assert fetched == ["MSFT", "GOOGL"]  # both filings, in order
    assert state.peer_metrics and state.peer_ticker == "GOOGL"
    assert analyst.peers_seen[0][0] == "GOOGL"  # the writer was told who the peer is
    assert "{{" not in state.memo
    assert "1.23x" in state.memo  # the peer value rendered
    assert "GOOGL: SEC filing" in state.memo
    assert "fiscal years end on different dates" in state.memo


def test_a_peer_placeholder_without_a_peer_is_caught(settings, facts, tmp_path):
    """Otherwise a memo could compare against a company that was never fetched."""
    analyst = FakeAnalyst([PEER_DRAFT] * 3)
    state = run_analysis(
        "msft", "How liquid is it?", analyst, settings, fetch=facts,
        runs_dir=tmp_path, fetch_text=None,
    )

    assert state.memo is None
    assert "no current_ratio for 2026 for the peer" in state.error


# --- citing the filing's narrative ---------------------------------------

def test_passages_reach_the_writer_and_the_memo_lists_what_was_cited(
    settings, facts, tmp_path
):
    """The retrieve node ranks the filing's paragraphs, so the test cites
    whichever one the search actually returns."""
    everything = load_passages(PASSAGES_FIXTURE)
    question = "Why did operating expenses increase?"
    expected = search(everything, question, 4)
    draft = f"Costs rose [{expected[0].id}]: the ratio {{{{current_ratio:2025->2026}}}}."

    analyst = FakeAnalyst([draft])
    state = run_analysis(
        "msft", question, analyst, settings, fetch=facts,
        runs_dir=tmp_path, fetch_text=lambda ticker: everything,
    )

    assert [p.id for p in state.passages] == [p.id for p in expected]
    assert analyst.passages_seen[0], "the writer was not given the passages"
    assert f"[{expected[0].id}]" in state.memo  # the citation survives rendering
    assert "**Cited sources**" in state.memo
    assert "Item 7" in state.memo
    # Only what was cited is listed, not everything retrieved.
    assert state.memo.count("filing 0001193125-26-323660") == 2  # one source line, one footer


def test_a_citation_to_a_passage_that_was_not_given_is_caught(settings, facts, tmp_path):
    analyst = FakeAnalyst(["Margins rose [P99]."] * 3)
    state = run_analysis(
        "msft", "Why?", analyst, settings, fetch=facts, runs_dir=tmp_path,
        fetch_text=lambda ticker: load_passages(PASSAGES_FIXTURE)[:2],
    )

    assert state.memo is None
    assert "[P99] is not a passage you were given" in state.error


def citing_run(settings, facts, tmp_path, drafts, verdicts=None, **kwargs):
    """A run with the filing's narrative, so the verify node has something to do."""
    everything = load_passages(PASSAGES_FIXTURE)
    analyst = FakeAnalyst(drafts, verdicts=verdicts)
    state = run_analysis(
        "msft", "Why did operating expenses increase?", analyst, settings, fetch=facts,
        runs_dir=tmp_path, fetch_text=lambda ticker: everything, **kwargs
    )
    return state, analyst


def cited_id(question="Why did operating expenses increase?"):
    return search(load_passages(PASSAGES_FIXTURE), question, 4)[0].id


def test_a_supported_claim_reaches_the_memo(settings, facts, tmp_path):
    draft = f"Costs rose on AI investment [{cited_id()}]."
    state, analyst = citing_run(settings, facts, tmp_path, [draft])

    assert state.memo and state.error is None
    assert len(analyst.claims_seen[0]) == 1  # the judge saw the one cited sentence
    assert state.cost_usd == pytest.approx(0.03)  # plan + write + verify


def test_an_unsupported_claim_is_sent_back_to_the_writer(settings, facts, tmp_path):
    bad = f"Costs rose because the CEO said so [{cited_id()}]."
    good = f"Costs rose on AI investment [{cited_id()}]."
    state, analyst = citing_run(
        settings, facts, tmp_path, [bad, good],
        verdicts=[[FakeVerdict(False, "the passage does not mention the CEO")]],
    )

    assert state.memo is not None
    assert len(state.drafts) == 2
    problems = analyst.problems_seen[1]
    assert "not supported by the passage it cites" in problems[0]
    assert "does not mention the CEO" in problems[0]


def test_claims_that_stay_unsupported_publish_nothing(settings, facts, tmp_path):
    bad = f"Costs rose because the CEO said so [{cited_id()}]."
    state, _ = citing_run(
        settings, facts, tmp_path, [bad] * 3,
        verdicts=[[FakeVerdict(False, "not in the passage")] for _ in range(3)],
    )

    assert state.memo is None
    assert "gave up after 3 drafts" in state.error
    assert "not supported" in state.error


def test_the_judge_is_skipped_when_nothing_is_cited(settings, facts, tmp_path):
    state, analyst = citing_run(settings, facts, tmp_path, [CLEAN_DRAFT])

    assert state.memo is not None
    assert analyst.claims_seen == [], "no citations, so nothing to check"
    assert state.cost_usd == pytest.approx(0.02)  # plan + write only


def test_verification_can_be_turned_off(settings, facts, tmp_path):
    draft = f"Costs rose on AI investment [{cited_id()}]."
    state, analyst = citing_run(settings, facts, tmp_path, [draft], verify=False)

    assert state.memo is not None
    assert analyst.claims_seen == []


def test_the_record_keeps_every_verdict_not_just_the_failures(settings, facts, tmp_path):
    """The record is the audit trail: what was checked matters as much as what failed."""
    draft = f"Costs rose on AI investment [{cited_id()}]."
    state, _ = citing_run(settings, facts, tmp_path, [draft])

    assert len(state.claim_checks) == 1
    check = state.claim_checks[0]
    assert check.supported and check.cited == [cited_id()]
    assert check.claim.endswith("].")

    record = json.loads(sorted(tmp_path.glob("*.json"))[0].read_text())
    assert record["claim_checks"][0]["supported"] is True


# --- the share price ------------------------------------------------------


def fake_quote(ticker):
    from fin_analyst.market import Quote

    fake_quote.calls.append(ticker)
    return Quote(ticker=ticker, price=500.0, as_of="2026-09-15", source="Alpha Vantage test")


def test_a_price_is_fetched_only_when_a_valuation_ratio_was_chosen(settings, facts, tmp_path):
    """No filing holds a price, and the call needs its own key, so it happens
    only when something actually needs it."""
    fake_quote.calls = []
    analyst = FakeAnalyst([CLEAN_DRAFT], metric_ids=["current_ratio"])
    run(analyst, settings, facts, tmp_path, quote=fake_quote)
    assert fake_quote.calls == []

    fake_quote.calls = []
    draft = "It trades at {{pe_ratio:2026}}."
    analyst = FakeAnalyst([draft], metric_ids=["pe_ratio"])
    state = run(analyst, settings, facts, tmp_path, quote=fake_quote)

    assert fake_quote.calls == ["MSFT"]
    assert "x" in state.memo  # the ratio rendered
    assert "Alpha Vantage test, 2026-09-15" in state.memo  # the footer dates the price


def test_a_missing_price_costs_the_valuation_ratios_and_nothing_else(settings, facts, tmp_path):
    from fin_analyst.market import MarketDataUnavailable

    def no_quote(ticker):
        raise MarketDataUnavailable("ALPHAVANTAGE_KEY is not set")

    draft = "Liquidity eased: the current ratio {{current_ratio:2025->2026}}."
    analyst = FakeAnalyst([draft], metric_ids=["pe_ratio", "current_ratio"])
    state = run(analyst, settings, facts, tmp_path, quote=no_quote)

    assert state.memo, "the rest of the memo should be unaffected"
    pe = next(m for m in state.metrics if m.metric_id == "pe_ratio")
    assert pe.value is None and "share_price" in pe.reason
