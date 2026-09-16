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

FIXTURE = Path(__file__).parent / "fixtures" / "msft_facts.json"

CLEAN_DRAFT = "Liquidity eased: the current ratio {{current_ratio:2025->2026}}."
LEAKY_DRAFT = "Liquidity eased: the current ratio fell to 1.23, roughly 2x cover."


class FakeAnalyst:
    """Returns scripted drafts and remembers what it was asked."""

    def __init__(self, drafts, metric_ids=("current_ratio",), cost=0.01):
        self.drafts = list(drafts)
        self.metric_ids = list(metric_ids)
        self.cost = cost
        self.problems_seen = []
        self.history_seen = []

    def choose_metrics(self, ticker, question):
        return self.metric_ids, self.cost

    def write_draft(self, ticker, question, metrics, problems, history=()):
        self.problems_seen.append(problems)
        self.history_seen.append(list(history))
        return self.drafts.pop(0), self.cost


@pytest.fixture
def facts():
    loaded = load_facts(FIXTURE)
    return lambda ticker: loaded


@pytest.fixture
def settings():
    return load_settings()


def run(analyst, settings, facts, tmp_path, question="How liquid is Microsoft?"):
    return run_analysis("msft", question, analyst, settings, fetch=facts, runs_dir=tmp_path)


def records_in(tmp_path):
    return sorted(tmp_path.glob("*.json"))


# --- the three paths through the graph -----------------------------------


def test_a_clean_draft_becomes_a_memo(settings, facts, tmp_path):
    state = run(FakeAnalyst([CLEAN_DRAFT]), settings, facts, tmp_path)

    assert state.error is None
    assert "fell 0.12x to 1.23x" in state.memo  # the renderer wrote the direction
    assert "{{" not in state.memo
    assert "Source: SEC filing 0001193125-26-323660" in state.memo  # footer is attached
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
