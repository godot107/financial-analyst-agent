"""Iteration 3, Phase B: the service on AWS Lambda.

One function, two jobs:

- **A request** arrives from the function URL (IAM-authenticated). The FastAPI
  app answers it through Mangum: health, coverage, job status, or a submission.
- **A job id** arrives from the function invoking itself asynchronously after a
  submission. The worker runs that one memo and writes the result to S3.

Submission returns in well under a second, so no request ever waits on a memo.

Secrets come from SSM Parameter Store at cold start, never from the image.
This file and server.py are the two entry points allowed to build the real
Claude analyst.
"""

import json
import os

from functools import partial

from fin_analyst.cache import S3Cache
from fin_analyst.config import load_settings
from fin_analyst.edgar import describe_filing, fetch_facts
from fin_analyst.jobs import S3JobStore
from fin_analyst.market import fetch_quote
from fin_analyst.news import fetch_news
from fin_analyst.passages import fetch_passages
from fin_analyst.server import parse_keys
from fin_analyst.service import Worker, create_app

SECRET_NAMES = ("ANTHROPIC_API_KEY", "SEC_USER_AGENT", "ALPHAVANTAGE_KEY", "FIN_ANALYST_API_KEYS")

_handler = None
_worker = None


def load_secrets(ssm, prefix: str) -> None:
    """Copy parameters under the prefix into the environment, where the SDKs look.

    Anything already set - even to an empty string - is left alone, which is how
    a local run of this image works without AWS.
    """
    wanted = {name for name in SECRET_NAMES if name not in os.environ}
    if not wanted:
        return
    token = None
    while True:
        kwargs = {"Path": prefix, "WithDecryption": True, "Recursive": False}
        if token:
            kwargs["NextToken"] = token
        page = ssm.get_parameters_by_path(**kwargs)
        for parameter in page.get("Parameters", []):
            name = parameter["Name"].rsplit("/", 1)[-1]
            if name in wanted:
                os.environ[name] = parameter["Value"]
        token = page.get("NextToken")
        if not token:
            return


def build(ssm=None, s3=None, lambda_client=None, analyst_factory=None, worker_kwargs=None):
    """Wire the app and worker. Tests pass fake clients and data sources; Lambda passes none."""
    import boto3
    from mangum import Mangum

    ssm = ssm or boto3.client("ssm")
    s3 = s3 or boto3.client("s3")
    lambda_client = lambda_client or boto3.client("lambda")

    load_secrets(ssm, os.environ.get("SSM_PREFIX", "/fin-analyst"))
    settings = load_settings()
    store = S3JobStore(os.environ["JOBS_BUCKET"], s3)

    if analyst_factory is None:
        from fin_analyst.llm import ClaudeAnalyst

        def analyst_factory():
            return ClaudeAnalyst(settings)

    # One bucket, two prefixes: jobs/ for the ledger, cache/ for filings, prices
    # and finished memos. Lambda's /tmp is wiped on every cold start; S3 is not.
    cache = S3Cache(os.environ["JOBS_BUCKET"], s3, prefix="cache/")
    sources = {
        "fetch": partial(fetch_facts, cache=cache),
        "fetch_text": partial(fetch_passages, cache=cache),
        "fetch_news": partial(fetch_news, cache=cache),
        "quote": partial(fetch_quote, cache=cache),
        "describe": describe_filing,
        "cache": cache,
    }
    worker = Worker(
        store,
        settings,
        analyst_factory=analyst_factory,
        runs_dir=_tmp("runs"),
        # Each step as a JSON line, so CloudWatch shows a memo being worked out
        # while it runs: aws logs tail /aws/lambda/fin-analyst --follow
        trace_log="json",
        **{**sources, **(worker_kwargs or {})},
    )

    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")

    def dispatch(job_id: str) -> None:
        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="Event",  # returns at once; the worker runs separately
            Payload=json.dumps({"job_id": job_id}).encode(),
        )

    keys = parse_keys(os.environ.get("FIN_ANALYST_API_KEYS", ""))
    app = create_app(store, settings, keys, worker, dispatch=dispatch)
    return Mangum(app, lifespan="off"), worker


def _tmp(name: str):
    from pathlib import Path

    path = Path("/tmp") / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def handler(event, context):
    global _handler, _worker
    if _handler is None:  # once per cold start
        _handler, _worker = build()

    if isinstance(event, dict) and "job_id" in event:
        job = _worker.process(str(event["job_id"]))
        return {"job_id": event["job_id"], "status": job.status if job else "missing"}

    return _handler(event, context)
