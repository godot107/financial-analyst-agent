"""Iteration 3, Phase A: the analyst as an HTTP service.

    POST /v1/memos          submit a memo job; 202 with an id and an estimate
    GET  /v1/memos/{id}     queued, running, done (memo and cost) or failed (why)
    POST /v1/batches        one question across tickers, with a required max_usd
    GET  /v1/coverage/{t}   which tags matched: synchronous, no model, free
    GET  /v1/health         liveness, and today's spend against the caps

A memo takes 30-90 seconds, longer than any gateway will hold a request open,
so submission returns at once and a single background worker does the work -
one job at a time, which also keeps traffic to the SEC serial.

Spend caps are enforced when a job is submitted, before any Claude call. An
unauthenticated or uncapped endpoint would be an open tab on the API account,
so every route but health needs a key.

No LLM import here: the analyst arrives through a factory, as everywhere else.
"""

import hmac
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from fin_analyst.batch import ESTIMATE_PER_MEMO, run_batch
from fin_analyst.config import Settings
from fin_analyst.coverage import concerns, line_item_coverage, metric_coverage
from fin_analyst.edgar import fetch_facts
from fin_analyst.graph import RUNS, run_analysis
from fin_analyst.jobs import Job, JobStore, next_reset
from fin_analyst.market import fetch_quote
from fin_analyst.news import fetch_news
from fin_analyst.passages import fetch_passages

TICKER = r"^[A-Za-z][A-Za-z.\-]{0,9}$"
# A memo without the filing's narrative and without the claim check.
ESTIMATE_RATIOS_ONLY = 0.03


class MemoRequest(BaseModel):
    ticker: str = Field(pattern=TICKER)
    question: str = Field(min_length=3, max_length=500)
    peer: str | None = Field(default=None, pattern=TICKER)
    text: bool = True  # cite the filing's narrative
    verify: bool = True  # check that citations hold
    news: bool = False  # add recent 8-K press releases
    market: bool = False  # fetch a price for valuation ratios

    def estimate_usd(self) -> float:
        return ESTIMATE_PER_MEMO if (self.text or self.verify) else ESTIMATE_RATIOS_ONLY


class BatchRequest(BaseModel):
    tickers: list[str] = Field(min_length=1, max_length=10)
    question: str = Field(min_length=3, max_length=500)
    max_usd: float = Field(gt=0, le=5)  # required: a batch must say what it may spend
    text: bool = True

    def estimate_usd(self) -> float:
        return min(len(self.tickers) * ESTIMATE_PER_MEMO, self.max_usd)


class Accepted(BaseModel):
    id: str
    status_url: str
    estimate_usd: float


class Worker:
    """Runs queued jobs one at a time. A job that fails is recorded, never raised."""

    def __init__(
        self,
        store: JobStore,
        settings: Settings,
        analyst_factory: Callable[[], object],
        runs_dir: Path = RUNS,
        fetch=fetch_facts,
        fetch_text=fetch_passages,
        fetch_news=fetch_news,
        quote=fetch_quote,
    ):
        self.store = store
        self.settings = settings
        self.analyst_factory = analyst_factory
        self.runs_dir = runs_dir
        self.fetch = fetch
        self.fetch_text = fetch_text
        self.fetch_news = fetch_news
        self.quote = quote

    def process_next(self) -> Job | None:
        """Run the oldest queued job, if there is one, and record how it ended."""
        job = self.store.claim_next()
        if job is None:
            return None

        # A fresh analyst per job, so its spend counter is this job's alone.
        analyst = self.analyst_factory()
        try:
            if job.kind == "memo":
                self._memo(job, analyst)
            else:
                self._batch(job, analyst)
        except Exception as failed:  # anything, so one bad job cannot stop the worker
            self.store.finish(
                job.id,
                "failed",
                cost_usd=getattr(analyst, "spent_usd", 0.0),
                error=f"{type(failed).__name__}: {failed}",
            )
        return self.store.get(job.id)

    def _memo(self, job: Job, analyst) -> None:
        request = MemoRequest(**job.request)
        state = run_analysis(
            request.ticker,
            request.question,
            analyst,
            self.settings,
            fetch=self.fetch,
            runs_dir=self.runs_dir,
            fetch_text=self.fetch_text if request.text else None,
            peer_ticker=request.peer,
            verify=request.verify,
            quote=self.quote if request.market else None,
            fetch_news=self.fetch_news if request.news else None,
        )
        self.store.finish(
            job.id,
            "done" if state.memo else "failed",
            # The analyst's own counter includes calls whose replies were rejected.
            cost_usd=getattr(analyst, "spent_usd", state.cost_usd),
            memo=state.memo,
            result={
                "metric_ids": state.metric_ids,
                "drafts": len(state.drafts),
                "claim_checks": [check.model_dump() for check in state.claim_checks],
            },
            error=state.error,
        )

    def _batch(self, job: Job, analyst) -> None:
        request = BatchRequest(**job.request)
        items = run_batch(
            request.tickers,
            request.question,
            analyst,
            self.settings,
            self.runs_dir / f"batch-{job.id}",
            fetch=self.fetch,
            fetch_text=self.fetch_text if request.text else None,
            max_usd_total=request.max_usd,
        )
        answered = sum(1 for item in items if item.memo_file)
        self.store.finish(
            job.id,
            "done" if answered else "failed",
            cost_usd=sum(item.cost_usd for item in items),
            result={"items": [item.model_dump() for item in items]},
            error=None if answered else "no company in the batch produced a memo",
        )

    def run_forever(self, stop: threading.Event, idle_seconds: float = 1.0) -> None:
        while not stop.is_set():
            if self.process_next() is None:
                stop.wait(idle_seconds)


def create_app(
    store: JobStore,
    settings: Settings,
    keys: dict[str, str],
    worker: Worker,
    run_worker: bool = False,
    fetch=fetch_facts,
) -> FastAPI:
    """The web app. Tests pass run_worker=False and drive the worker by hand."""
    if not keys:
        raise ValueError("refusing to start without at least one API key")

    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        thread = None
        if run_worker:
            thread = threading.Thread(target=worker.run_forever, args=(stop,), daemon=True)
            thread.start()
        yield
        stop.set()
        if thread:
            thread.join(timeout=5)

    app = FastAPI(title="financial-analyst-agent", version="3.0-phase-a", lifespan=lifespan)

    def caller(x_api_key: str | None = Header(default=None)) -> str:
        """The key's name, or 401. Compared in constant time."""
        if x_api_key:
            for name, secret in keys.items():
                if hmac.compare_digest(x_api_key, secret):
                    return name
        raise HTTPException(status_code=401, detail="a valid X-API-Key header is required")

    def admit(key_name: str, estimate: float) -> None:
        """Refuse before spending, not after."""
        key_left = settings.api_per_key_daily_usd - store.committed_today(key_name)
        global_left = settings.api_global_daily_usd - store.committed_today()
        if estimate > min(key_left, global_left):
            which = "this key's" if key_left <= global_left else "the service's"
            raise HTTPException(
                status_code=429,
                detail={
                    "error": f"{which} daily spending cap would be exceeded",
                    "estimate_usd": round(estimate, 4),
                    "remaining_usd": round(max(0.0, min(key_left, global_left)), 4),
                    "resets_at": next_reset(),
                },
            )

    @app.get("/v1/health")
    def health():
        return {
            "status": "ok",
            "spent_today_usd": round(store.committed_today(), 4),
            "global_daily_cap_usd": settings.api_global_daily_usd,
        }

    @app.post("/v1/memos", status_code=202, response_model=Accepted)
    def submit_memo(request: MemoRequest, key_name: str = Depends(caller)):
        estimate = request.estimate_usd()
        admit(key_name, estimate)
        job = store.add("memo", key_name, request.model_dump(), estimate)
        return Accepted(id=job.id, status_url=f"/v1/memos/{job.id}", estimate_usd=estimate)

    @app.post("/v1/batches", status_code=202, response_model=Accepted)
    def submit_batch(request: BatchRequest, key_name: str = Depends(caller)):
        estimate = request.estimate_usd()
        admit(key_name, estimate)
        job = store.add("batch", key_name, request.model_dump(), estimate)
        return Accepted(id=job.id, status_url=f"/v1/memos/{job.id}", estimate_usd=estimate)

    @app.get("/v1/memos/{job_id}", response_model=Job)
    def read_job(job_id: str, key_name: str = Depends(caller)):
        job = store.get(job_id)
        # Someone else's job is reported as missing, not forbidden: an id alone
        # should not confirm that a job exists.
        if job is None or job.key_name != key_name:
            raise HTTPException(status_code=404, detail="no such job")
        return job

    @app.get("/v1/coverage/{ticker}")
    def coverage(ticker: str, key_name: str = Depends(caller)):
        facts = fetch(ticker.upper())
        return {
            "ticker": ticker.upper(),
            "line_items": [row.model_dump() for row in line_item_coverage(facts)],
            "metrics": [row.model_dump() for row in metric_coverage(facts)],
            "concerns": concerns(facts),
        }

    return app
