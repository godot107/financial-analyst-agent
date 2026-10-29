"""The memo service on Amazon Bedrock AgentCore Runtime (docs/AGENTCORE_PLAN.md).

The same worker, job ledger, caches and daily caps as the Lambda stack
(lambda_handler.py). What changes is how a memo gets run:

- **Lambda** answers a submission at once and invokes itself to run the memo,
  because a function URL request can't wait out a memo.
- **Runtime** sessions may run for a long time, so `/invocations` runs the memo
  in the request and returns the finished job. No self-invoke, no polling.

AgentCore's container contract: HTTP on 0.0.0.0:8080, `POST /invocations`,
`GET /ping`. `/ping` answers "HealthyBusy" while a memo runs, so Runtime doesn't
treat the session as idle. Callers are authenticated by AgentCore (IAM SigV4 on
InvokeAgentRuntime), so there is no API key here.

Claude runs on Bedrock (FIN_ANALYST_PROVIDER=bedrock, set by CloudFormation),
with the Runtime's role: no model key anywhere. Like server.py and
lambda_handler.py, this is an entry point allowed to build the real analyst.

    python -m fin_analyst.agentcore_app      # local: JOBS_BUCKET, AWS credentials
"""

import os
import threading
from functools import partial
from typing import Callable

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ValidationError

from fin_analyst.cache import S3Cache
from fin_analyst.config import Settings, load_settings
from fin_analyst.edgar import describe_filing, fetch_facts
from fin_analyst.jobs import Job, S3JobStore, next_reset
from fin_analyst.market import fetch_quote
from fin_analyst.news import fetch_news
from fin_analyst.passages import fetch_passages
from fin_analyst.service import MemoRequest, Worker

CALLER = r"^[a-z][a-z0-9-]{0,31}$"


class Invocation(MemoRequest):
    """A memo request, plus who it's for. AgentCore has already checked the caller's
    IAM signature; `caller` only splits the daily cap between programs."""

    caller: str = Field(default="default", pattern=CALLER)


def create_app(store, settings: Settings, worker: Worker) -> FastAPI:
    app = FastAPI(title="financial-analyst-agent (AgentCore)", version="4.0-phase-0")
    busy = threading.Event()

    @app.get("/ping")
    def ping():
        return {"status": "HealthyBusy" if busy.is_set() else "Healthy"}

    @app.post("/invocations")
    def invoke(payload: dict) -> dict:
        try:
            request = Invocation.model_validate(payload)
        except ValidationError as invalid:
            raise HTTPException(status_code=422, detail=invalid.errors(include_url=False)) from None

        # The same caps as the Lambda stack, checked before anything is spent.
        estimate = request.estimate_usd()
        key_left = settings.api_per_key_daily_usd - store.committed_today(request.caller)
        global_left = settings.api_global_daily_usd - store.committed_today()
        if estimate > min(key_left, global_left):
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "the daily spending cap would be exceeded",
                    "remaining_usd": round(max(0.0, min(key_left, global_left)), 4),
                    "resets_at": next_reset(),
                },
            )

        job = store.add("memo", request.caller, request.model_dump(exclude={"caller"}), estimate)
        busy.set()
        try:
            finished: Job | None = worker.process(job.id)
        finally:
            busy.clear()
        return finished.model_dump(mode="json") if finished else {"id": job.id, "status": "missing"}

    return app


def build(s3=None, analyst_factory: Callable | None = None, worker_kwargs: dict | None = None):
    """Wire the app. Tests pass a fake S3 and fake sources; the Runtime passes none."""
    import boto3

    s3 = s3 or boto3.client("s3")
    settings = load_settings()
    if settings.provider != "bedrock":
        raise RuntimeError("the AgentCore app runs Claude on Bedrock: set FIN_ANALYST_PROVIDER=bedrock")
    bucket = os.environ["JOBS_BUCKET"]
    store = S3JobStore(bucket, s3)

    if analyst_factory is None:
        from fin_analyst.llm import ClaudeAnalyst

        def analyst_factory():
            return ClaudeAnalyst(settings)

    cache = S3Cache(bucket, s3, prefix="cache/")
    sources = {
        "fetch": partial(fetch_facts, cache=cache),
        "fetch_text": partial(fetch_passages, cache=cache),
        "fetch_news": partial(fetch_news, cache=cache),
        # The Alpha Vantage key arrives through AgentCore Identity in Phase 3; until
        # then a market request fails as "no key" and is recorded, like any failure.
        "quote": partial(fetch_quote, cache=cache),
        "describe": describe_filing,
        "cache": cache,
    }
    worker = Worker(
        store,
        settings,
        analyst_factory=analyst_factory,
        runs_dir=_tmp("runs"),
        trace_log="json",  # one JSON line per step, to the Runtime's CloudWatch log
        **{**sources, **(worker_kwargs or {})},
    )
    return create_app(store, settings, worker)


def _tmp(name: str):
    from pathlib import Path

    path = Path("/tmp") / name
    path.mkdir(parents=True, exist_ok=True)
    return path


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(build(), host="0.0.0.0", port=8080)
