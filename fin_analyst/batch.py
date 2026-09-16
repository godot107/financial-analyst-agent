"""One question, several companies, one folder of memos.

This is the repetitive part: the same question across a watchlist, which is also
the regression check that a tagging problem in one filing hasn't spread. A
company that fails does not stop the run - its reason is recorded and the batch
carries on - and a total spend cap sits above the per-memo one.

No LLM import here: the analyst is passed in, like everywhere else.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from fin_analyst.config import Settings
from fin_analyst.edgar import Fact, fetch_facts
from fin_analyst.graph import RUNS, Analyst, run_analysis
from fin_analyst.passages import Passage, fetch_passages

# What a memo has cost in practice: about $0.03 without the filing's narrative,
# about $0.06 with it. Used only to estimate before spending.
ESTIMATE_PER_MEMO = 0.06


class BatchItem(BaseModel):
    ticker: str
    memo_file: str | None = None
    error: str | None = None
    cost_usd: float = 0.0
    drafts: int = 0


def batch_dir(parent: Path = RUNS) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return parent / f"batch-{stamp}"


def run_batch(
    tickers: list[str],
    question: str,
    analyst: Analyst,
    settings: Settings,
    out_dir: Path,
    fetch: Callable[[str], list[Fact]] = fetch_facts,
    fetch_text: Callable[[str], list[Passage]] | None = fetch_passages,
    max_usd_total: float | None = None,
) -> list[BatchItem]:
    """A memo per ticker, plus a summary. Failures are recorded, not raised."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = settings.max_usd_per_run if max_usd_total is None else max_usd_total

    items: list[BatchItem] = []
    spent = 0.0
    for ticker in tickers:
        ticker = ticker.upper()
        if spent >= cap:
            items.append(BatchItem(ticker=ticker, error=f"skipped: the batch reached ${cap:.2f}"))
            continue

        try:
            state = run_analysis(
                ticker, question, analyst, settings,
                fetch=fetch, runs_dir=out_dir, fetch_text=fetch_text,
            )
        except Exception as failed:  # a bad ticker must not end the batch
            items.append(BatchItem(ticker=ticker, error=f"{type(failed).__name__}: {failed}"))
            continue

        spent += state.cost_usd
        memo_file = next(
            (path.name for path in sorted(out_dir.glob(f"{ticker}-*.md"), reverse=True)), None
        )
        items.append(
            BatchItem(
                ticker=ticker,
                memo_file=memo_file if state.memo else None,
                error=state.error,
                cost_usd=state.cost_usd,
                drafts=len(state.drafts),
            )
        )

    (out_dir / "summary.md").write_text(summarize(items, question))
    return items


def summarize(items: list[BatchItem], question: str) -> str:
    """The summary a person reads first: who answered, who didn't, what it cost."""
    lines = [
        f"# Batch: {question}",
        "",
        "| ticker | result | drafts | cost |",
        "|---|---|---|---|",
    ]
    for item in items:
        result = item.memo_file or (item.error or "no memo")
        lines.append(f"| {item.ticker} | {result} | {item.drafts} | ${item.cost_usd:.4f} |")

    answered = sum(1 for i in items if i.memo_file)
    lines += [
        "",
        f"{answered}/{len(items)} answered. Total ${sum(i.cost_usd for i in items):.4f}.",
    ]
    return "\n".join(lines) + "\n"
