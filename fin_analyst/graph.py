"""Step 4: the workflow.

    START → plan → fetch → compute → retrieve → write → check → verify ──ok──→ render → END
                                                   ▲          │        │
                                                   └──retry───┴────────┘
                                                              └──gave up──→ END (no memo)

`check` is deterministic: leaked numbers, unknown placeholders, dangling citations.
`verify` asks Claude whether each cited claim is actually in the passage it cites.

Claude appears twice, behind the `Analyst` interface below, so tests can run the
whole graph with a scripted stand-in and no API key. Everything else is Python.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from fin_analyst.config import Settings
from fin_analyst.edgar import Fact, fetch_facts
from fin_analyst.memo import build_footer, cited_claims, find_problems, render
from fin_analyst.market import MarketDataUnavailable, fetch_quote, price_fact
from fin_analyst.metrics import MARKET_METRIC_IDS, METRICS_BY_ID, MetricResult, compute_all
from fin_analyst.passages import Passage, fetch_passages, search

RUNS = Path(__file__).resolve().parent.parent / "runs"


class ClaimCheck(BaseModel):
    """What the judge said about one cited claim, kept for the record."""

    claim: str
    cited: list[str]
    supported: bool
    reason: str


class AnalysisState(BaseModel):
    """What the workflow knows, at every point along the way."""

    ticker: str
    question: str
    # The company to compare against, if any. Its ratios are computed the same
    # way and reach the writer as {{peer.metric:year}} placeholders.
    peer_ticker: str | None = None
    metric_ids: list[str] = Field(default_factory=list)  # plan
    facts: list[Fact] = Field(default_factory=list)  # fetch
    peer_facts: list[Fact] = Field(default_factory=list)
    metrics: list[MetricResult] = Field(default_factory=list)  # compute
    peer_metrics: list[MetricResult] = Field(default_factory=list)
    # retrieve: the few filing paragraphs that bear on the question, which is
    # the only way the memo may explain *why* a number moved.
    passages: list[Passage] = Field(default_factory=list)
    # verify: every verdict, not only the failures, so the run record shows what
    # was checked as well as what was rejected.
    claim_checks: list[ClaimCheck] = Field(default_factory=list)
    drafts: list[str] = Field(default_factory=list)  # write: every attempt, latest last
    problems: list[str] = Field(default_factory=list)  # check: latest draft's problems
    memo: str | None = None  # render
    # Earlier questions and answers in a --chat session, oldest first. Empty for
    # a one-shot run.
    history: list[tuple[str, str]] = Field(default_factory=list)
    cost_usd: float = 0.0  # this run, plus anything spent earlier in the session
    error: str | None = None  # gave up, or ran out of budget


class Analyst(Protocol):
    """The two things Claude does. `llm.py` implements this; tests fake it."""

    def choose_metrics(self, ticker: str, question: str) -> tuple[list[str], float]:
        """Metric ids to compute, and what the call cost."""

    def verify_claims(self, claims) -> tuple[list, float]:
        """One verdict per cited claim: is the passage it cites behind it?"""

    def write_draft(
        self,
        ticker: str,
        question: str,
        metrics: list[MetricResult],
        problems: list[str],
        history: list[tuple[str, str]] = (),
        peer_ticker: str | None = None,
        peer_metrics: list[MetricResult] = (),
        passages: list[Passage] = (),
    ) -> tuple[str, float]:
        """A memo draft written in placeholders, and what the call cost."""


class AnalysisFailed(RuntimeError):
    """A run stopped for a reason worth recording rather than crashing on."""


class BudgetExceeded(AnalysisFailed):
    """A run spent more than config.yaml allows. Something is wrong; stop."""


def build_graph(
    analyst: Analyst,
    settings: Settings,
    fetch: Callable[[str], list[Fact]] = fetch_facts,
    fetch_text: Callable[[str], list[Passage]] | None = fetch_passages,
    passages_k: int = 4,
    verify: bool = True,
    quote: Callable[[str], object] | None = None,
    fetch_news: Callable[[str], list[Passage]] | None = None,
    news_k: int = 3,
):
    """Wire the six nodes. Nothing here talks to a model except through `analyst`."""

    def spend(state: AnalysisState, cost: float) -> float:
        total = state.cost_usd + cost
        if total > settings.max_usd_per_run:
            raise BudgetExceeded(
                f"this run reached ${total:.2f}, over the ${settings.max_usd_per_run:.2f} limit"
            )
        return total

    def plan(state: AnalysisState) -> dict:
        metric_ids, cost = analyst.choose_metrics(state.ticker, state.question)
        unknown = [i for i in metric_ids if i not in METRICS_BY_ID]
        if unknown:
            raise ValueError(f"the plan asked for metrics that do not exist: {unknown}")
        return {"metric_ids": metric_ids, "cost_usd": spend(state, cost)}

    def fetch_node(state: AnalysisState) -> dict:
        facts = fetch(state.ticker)
        if not facts:
            raise ValueError(f"no facts found in the latest 10-K for {state.ticker}")

        # A share price, but only when a valuation ratio was actually asked for:
        # no filing contains one, and the call needs its own key.
        if quote and MARKET_METRIC_IDS & set(state.metric_ids):
            latest = max(f.fiscal_year for f in facts)
            try:
                facts = facts + [price_fact(quote(state.ticker), latest)]
            except MarketDataUnavailable as unavailable:
                # Not fatal: the valuation ratios report themselves unavailable,
                # and the rest of the memo is unaffected.
                print(f"  (no market data: {unavailable})")

        update = {"facts": facts}
        if state.peer_ticker:
            peer_facts = fetch(state.peer_ticker)
            if not peer_facts:
                raise ValueError(f"no facts found in the latest 10-K for {state.peer_ticker}")
            update["peer_facts"] = peer_facts
        return update

    def compute(state: AnalysisState) -> dict:
        return {
            "metrics": compute_all(state.facts, state.metric_ids),
            "peer_metrics": compute_all(state.peer_facts, state.metric_ids),
        }

    def retrieve(state: AnalysisState) -> dict:
        """The filing's narrative, narrowed to the question.

        Only the subject company: a peer's narrative would double the reading
        for a comparison the ratios already carry.
        """
        passages = []
        if fetch_text:
            passages += search(fetch_text(state.ticker), state.question, passages_k)
        if fetch_news:
            # Anything after the filing's year end, cited the same way and held
            # to the same rules: words only, and every claim carries its source.
            passages += search(fetch_news(state.ticker), state.question, news_k)
        return {"passages": passages}

    def write(state: AnalysisState) -> dict:
        draft, cost = analyst.write_draft(
            state.ticker,
            state.question,
            state.metrics,
            state.problems,
            state.history,
            state.peer_ticker,
            state.peer_metrics,
            state.passages,
        )
        return {"drafts": state.drafts + [draft], "cost_usd": spend(state, cost)}

    def check(state: AnalysisState) -> dict:
        return {
            "problems": find_problems(
                state.drafts[-1], state.metrics, state.peer_metrics, state.passages
            )
        }

    def verify_node(state: AnalysisState) -> dict:
        """Ask Claude whether each cited claim is really in the passage it cites.

        The checker before this proves the citation exists; this asks whether it
        holds the claim up. An unsupported claim is handled like any other
        problem: the writer is told, and rewrites.
        """
        claims = cited_claims(state.drafts[-1], state.passages)
        verdicts, cost = analyst.verify_claims(claims)

        checks = [
            ClaimCheck(
                claim=claim,
                cited=[p.id for p in passages],
                supported=verdict.supported,
                reason=verdict.reason,
            )
            for (claim, passages), verdict in zip(claims, verdicts)
        ]
        problems = [
            f'this claim is not supported by the passage it cites: "{check.claim}" - {check.reason}'
            for check in checks
            if not check.supported
        ]
        return {
            "claim_checks": state.claim_checks + checks,
            "problems": problems,
            "cost_usd": spend(state, cost),
        }

    def after_check(state: AnalysisState) -> str:
        if state.problems:
            return "write" if len(state.drafts) <= settings.max_retries else "gave_up"
        # Nothing wrong with the mechanics; now ask whether the citations hold.
        return "verify" if verify and cited_claims(state.drafts[-1], state.passages) else "render"

    def after_verify(state: AnalysisState) -> str:
        if not state.problems:
            return "render"
        return "write" if len(state.drafts) <= settings.max_retries else "gave_up"

    def gave_up(state: AnalysisState) -> dict:
        return {
            "error": (
                f"gave up after {len(state.drafts)} drafts; the last one still had: "
                + "; ".join(state.problems)
            )
        }

    def render_node(state: AnalysisState) -> dict:
        footer = build_footer(
            state.facts, state.metrics, state.ticker, state.peer_facts, state.peer_ticker,
            state.passages,
        )
        return {
            "memo": render(
                state.drafts[-1], state.metrics, footer, state.peer_metrics, state.passages
            )
        }

    graph = StateGraph(AnalysisState)
    graph.add_node("plan", plan)
    graph.add_node("fetch", fetch_node)
    graph.add_node("compute", compute)
    graph.add_node("retrieve", retrieve)
    graph.add_node("write", write)
    graph.add_node("check", check)
    graph.add_node("verify", verify_node)
    graph.add_node("gave_up", gave_up)
    graph.add_node("render", render_node)

    graph.add_edge(START, "plan")
    graph.add_edge("plan", "fetch")
    graph.add_edge("fetch", "compute")
    graph.add_edge("compute", "retrieve")
    graph.add_edge("retrieve", "write")
    graph.add_edge("write", "check")
    graph.add_conditional_edges(
        "check",
        after_check,
        {"render": "render", "write": "write", "verify": "verify", "gave_up": "gave_up"},
    )
    graph.add_conditional_edges(
        "verify",
        after_verify,
        {"render": "render", "write": "write", "gave_up": "gave_up"},
    )
    graph.add_edge("render", END)
    graph.add_edge("gave_up", END)
    return graph.compile()


def run_analysis(
    ticker: str,
    question: str,
    analyst: Analyst,
    settings: Settings,
    fetch: Callable[[str], list[Fact]] = fetch_facts,
    runs_dir: Path = RUNS,
    fetch_text: Callable[[str], list[Passage]] | None = fetch_passages,
    history: list[tuple[str, str]] = (),
    cost_so_far: float = 0.0,
    peer_ticker: str | None = None,
    verify: bool = True,
    quote: Callable[[str], object] | None = None,
    fetch_news: Callable[[str], list[Passage]] | None = None,
) -> AnalysisState:
    """Run the workflow and save what happened, memo or no memo.

    `history` and `cost_so_far` carry a --chat session across turns, so the
    budget guard covers the whole conversation rather than each turn separately.
    """
    state = AnalysisState(
        ticker=ticker.upper(),
        question=question,
        peer_ticker=peer_ticker.upper() if peer_ticker else None,
        history=list(history),
        cost_usd=cost_so_far,
    )
    graph = build_graph(
        analyst, settings, fetch, fetch_text, verify=verify, quote=quote, fetch_news=fetch_news
    )

    try:
        state = AnalysisState.model_validate(graph.invoke(state))
    except AnalysisFailed as stopped:
        state = state.model_copy(update={"error": str(stopped)})

    save_run(state, runs_dir)
    return state


def save_run(state: AnalysisState, runs_dir: Path = RUNS) -> Path:
    """The record of a run: what was asked, every draft, what was wrong, what it cost.

    Written whether or not a memo came out, because the failures are the
    interesting ones.
    """
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{state.ticker}-{stamp}"

    record = runs_dir / f"{stem}.json"
    record.write_bytes(
        state.model_dump_json(indent=2, exclude={"facts", "peer_facts"}).encode() + b"\n"
    )
    if state.memo:
        (runs_dir / f"{stem}.md").write_text(state.memo + "\n")
    return record
