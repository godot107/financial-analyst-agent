"""Run the HTTP service: python -m fin_analyst.server

Reads API keys from FIN_ANALYST_API_KEYS as "name=secret,name2=secret2" and
refuses to start without one. Binds to 127.0.0.1 unless HOST says otherwise.

This is the one place besides __main__ that builds the real Claude analyst.
"""

import os
import sys
from functools import partial

import uvicorn
from dotenv import load_dotenv

from fin_analyst.cache import LocalCache
from fin_analyst.config import PROJECT_ROOT, load_settings
from fin_analyst.edgar import describe_filing, fetch_facts
from fin_analyst.graph import RUNS
from fin_analyst.jobs import JobStore
from fin_analyst.llm import ClaudeAnalyst
from fin_analyst.market import fetch_quote
from fin_analyst.news import fetch_news
from fin_analyst.passages import fetch_passages
from fin_analyst.service import Worker, create_app


def parse_keys(raw: str) -> dict[str, str]:
    keys = {}
    for pair in filter(None, (part.strip() for part in raw.split(","))):
        name, _, secret = pair.partition("=")
        if not name or len(secret) < 16:
            raise ValueError(f"API key '{name or pair}' must be name=secret, with a secret of 16+ characters")
        keys[name] = secret
    return keys


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        keys = parse_keys(os.environ.get("FIN_ANALYST_API_KEYS", ""))
    except ValueError as bad:
        print(bad, file=sys.stderr)
        return 1
    if not keys:
        print("Set FIN_ANALYST_API_KEYS (name=secret,...); the service will not run open.", file=sys.stderr)
        return 1

    settings = load_settings()
    RUNS.mkdir(parents=True, exist_ok=True)
    store = JobStore(os.environ.get("FIN_ANALYST_JOBS_DB", str(RUNS / "jobs.sqlite3")))
    cache = LocalCache(PROJECT_ROOT / "cache")
    worker = Worker(
        store,
        settings,
        analyst_factory=lambda: ClaudeAnalyst(settings),
        fetch=partial(fetch_facts, cache=cache),
        fetch_text=partial(fetch_passages, cache=cache),
        fetch_news=partial(fetch_news, cache=cache),
        quote=partial(fetch_quote, cache=cache),
        cache=cache,
        describe=describe_filing,
        trace_log="pretty",  # each job's steps and thinking, in this terminal
    )
    app = create_app(store, settings, keys, worker, run_worker=True)

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
