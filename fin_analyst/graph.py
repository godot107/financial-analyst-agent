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

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from fin_analyst.config import Settings
from fin_analyst.edgar import Fact, Filing, fetch_facts
from fin_analyst.memo import build_footer, cited_claims, find_problems, render
from fin_analyst.market import MarketDataUnavailable, fetch_quote, price_fact
from fin_analyst.metrics import MARKET_METRIC_IDS, METRICS_BY_ID, MetricResult, compute_all
from fin_analyst.passages import Passage, fetch_passages, name_words, search
from fin_analyst.trace import TraceEvent, Tracer

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
    # fetch: which filing, when a describer was given; None when it wasn't, or
    # when it named a different filing from the one the facts came from.
    filing: Filing | None = None
    peer_filing: Filing | None = None
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
    # Every step as it happened, including Claude's summarized thinking.
    trace: list[TraceEvent] = Field(default_factory=list)


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
    tracer: Tracer | None = None,
    describe: Callable[[str], Filing] | None = None,
):
    """Wire the six nodes. Nothing here talks to a model except through `analyst`."""
    tracer = tracer or Tracer()

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
                tracer.emit("fetch", "no market data", reason=str(unavailable))

        update = {"facts": facts, "filing": describe_one(state.ticker, facts)}
        if state.peer_ticker:
            peer_facts = fetch(state.peer_ticker)
            if not peer_facts:
                raise ValueError(f"no facts found in the latest 10-K for {state.peer_ticker}")
            update["peer_facts"] = peer_facts
            update["peer_filing"] = describe_one(state.peer_ticker, peer_facts)
        return update

    def describe_one(ticker: str, facts: list[Fact]) -> Filing | None:
        """Who filed the facts, and when. Nice to have, so never fatal."""
        if describe is None:
            return None
        try:
            filing = describe(ticker)
        except Exception as failed:
            tracer.emit("fetch", "no filing details", ticker=ticker, reason=str(failed))
            return None
        # A separate lookup of "the latest 10-K": if a new one landed in between,
        # these details would describe a filing the numbers didn't come from.
        if filing.accession not in {f.accession for f in facts}:
            tracer.emit("fetch", "no filing details", ticker=ticker,
                        reason=f"{filing.accession} is not the filing the facts came from")
            return None
        periods = [f.period for f in facts if f.line_item == "total_assets"]
        return filing.model_copy(update={"period": max(periods, default=None)})

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
        # The company's own name is in the question and all over its filing, so
        # it matches paragraphs for no reason. Search without it.
        ignore = name_words(state.filing.company if state.filing else None)
        if fetch_text:
            passages += search(fetch_text(state.ticker), state.question, passages_k, ignore)
        if fetch_news:
            # Anything after the filing's year end, cited the same way and held
            # to the same rules: words only, and every claim carries its source.
            passages += search(fetch_news(state.ticker), state.question, news_k, ignore)
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
            route = "write" if len(state.drafts) <= settings.max_retries else "gave_up"
        # Nothing wrong with the mechanics; now ask whether the citations hold.
        elif verify and cited_claims(state.drafts[-1], state.passages):
            route = "verify"
        else:
            route = "render"
        tracer.emit("route", f"check -> {route}", problems=len(state.problems))
        return route

    def after_verify(state: AnalysisState) -> str:
        if not state.problems:
            route = "render"
        else:
            route = "write" if len(state.drafts) <= settings.max_retries else "gave_up"
        tracer.emit("route", f"verify -> {route}", problems=len(state.problems))
        return route

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

    def traced(name: str, node: Callable[[AnalysisState], dict]):
        """Time a node and record what it produced, or why it failed."""

        def run(state: AnalysisState) -> dict:
            started = time.monotonic()
            try:
                update = node(state)
            except Exception as failed:
                tracer.emit(name, "failed", seconds=round(time.monotonic() - started, 2), error=str(failed))
                raise
            tracer.emit(
                name, "done", seconds=round(time.monotonic() - started, 2), **summarize(name, state, update)
            )
            return update

        return run

    graph = StateGraph(AnalysisState)
    graph.add_node("plan", traced("plan", plan))
    graph.add_node("fetch", traced("fetch", fetch_node))
    graph.add_node("compute", traced("compute", compute))
    graph.add_node("retrieve", traced("retrieve", retrieve))
    graph.add_node("write", traced("write", write))
    graph.add_node("check", traced("check", check))
    graph.add_node("verify", traced("verify", verify_node))
    graph.add_node("gave_up", traced("gave_up", gave_up))
    graph.add_node("render", traced("render", render_node))

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


def summarize(name: str, state: AnalysisState, update: dict) -> dict:
    """The part of a node's output worth reading in a trace, kept short."""
    if name == "plan":
        return {"metric_ids": update["metric_ids"]}
    if name == "fetch":
        years = sorted({f.fiscal_year for f in update["facts"]})
        filing = update.get("filing")
        return {
            "filing": f"{filing.company} {filing.form} filed {filing.filed} ({filing.accession})" if filing else None,
            "facts": len(update["facts"]),
            "fiscal_years": years,
            "peer_facts": len(update.get("peer_facts", [])) or None,
        }
    if name == "compute":
        def show(metrics, prefix=""):
            return [
                f"{prefix}{m.metric_id}:{m.fiscal_year} = "
                + (f"unavailable ({m.reason})" if m.value is None else f"{m.value:.4f}")
                for m in metrics
            ]
        return {"values": show(update["metrics"]) + show(update["peer_metrics"], "peer.")}
    if name == "retrieve":
        return {
            "passages": [f"[{p.id}] {p.item}: {p.text[:140]}..." for p in update["passages"]]
        }
    if name == "write":
        return {"attempt": len(update["drafts"]), "draft": update["drafts"][-1]}
    if name == "check":
        return {"problems": update["problems"]}
    if name == "verify":
        new = update["claim_checks"][len(state.claim_checks):]
        return {
            "verdicts": [
                f"{'supported' if c.supported else 'NOT supported'} {c.cited}: {c.reason}" for c in new
            ]
        }
    if name == "gave_up":
        return {"error": update["error"]}
    if name == "render":
        return {"memo_chars": len(update["memo"])}
    return {}


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
    tracer: Tracer | None = None,
    describe: Callable[[str], Filing] | None = None,
) -> AnalysisState:
    """Run the workflow and save what happened, memo or no memo.

    `history` and `cost_so_far` carry a --chat session across turns, so the
    budget guard covers the whole conversation rather than each turn separately.
    `describe` looks up who filed the 10-K and when (`edgar.describe_filing`).
    """
    state = AnalysisState(
        ticker=ticker.upper(),
        question=question,
        peer_ticker=peer_ticker.upper() if peer_ticker else None,
        history=list(history),
        cost_usd=cost_so_far,
    )
    tracer = tracer or Tracer()
    if hasattr(analyst, "tracer"):
        analyst.tracer = tracer  # so its calls land in this run's trace
    first_event = len(tracer.events)
    tracer.emit(
        "run", "start", ticker=state.ticker, question=question, peer=state.peer_ticker,
        text=fetch_text is not None, verify=verify, news=fetch_news is not None,
        market=quote is not None,
    )
    graph = build_graph(
        analyst, settings, fetch, fetch_text, verify=verify, quote=quote, fetch_news=fetch_news,
        tracer=tracer, describe=describe,
    )

    try:
        state = AnalysisState.model_validate(graph.invoke(state))
    except AnalysisFailed as stopped:
        state = state.model_copy(update={"error": str(stopped)})

    tracer.emit(
        "run", "done" if state.memo else "failed",
        drafts=len(state.drafts), cost_usd=round(getattr(analyst, "spent_usd", state.cost_usd), 5),
        error=state.error,
    )
    state = state.model_copy(update={"trace": tracer.events[first_event:]})

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
