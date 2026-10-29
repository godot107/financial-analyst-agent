"""The AgentCore Runtime entry point, with a fake S3 and a fake Claude: nothing touches AWS."""

import threading

import pytest
from fastapi.testclient import TestClient

from fin_analyst.agentcore_app import build
from tests.test_graph import CLEAN_DRAFT, FakeAnalyst
from tests.test_lambda import FACTS, FakeS3


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setenv("FIN_ANALYST_PROVIDER", "bedrock")
    monkeypatch.setenv("JOBS_BUCKET", "fin-analyst-agentcore-test")

    def make(analyst=None):
        s3 = FakeS3()
        app = build(
            s3=s3,
            analyst_factory=analyst or (lambda: FakeAnalyst([CLEAN_DRAFT])),
            worker_kwargs={
                "fetch": lambda ticker: FACTS,
                "fetch_text": lambda ticker: [],
                "fetch_news": lambda ticker: [],
                "quote": None,
                "lookup_accession": lambda ticker: f"acc-{ticker}",
                "describe": None,
            },
        )
        return TestClient(app), s3

    return make


def test_ping_is_healthy_when_idle(runtime):
    client, _ = runtime()
    assert client.get("/ping").json() == {"status": "Healthy"}


def test_an_invocation_runs_the_memo_and_returns_the_finished_job(runtime):
    client, s3 = runtime()
    reply = client.post("/invocations", json={"ticker": "MSFT", "question": "How liquid is it?",
                                              "text": False, "verify": False})
    assert reply.status_code == 200
    job = reply.json()
    assert job["status"] == "done" and job["memo"]
    assert job["key_name"] == "default"
    assert any(key.startswith("jobs/") for key in s3.objects)  # the same ledger as Lambda


def test_ping_reports_busy_while_a_memo_runs(runtime):
    started, release = threading.Event(), threading.Event()

    class SlowAnalyst(FakeAnalyst):
        def choose_metrics(self, ticker, question):
            started.set()
            release.wait(5)
            return super().choose_metrics(ticker, question)

    client, _ = runtime(analyst=lambda: SlowAnalyst([CLEAN_DRAFT]))
    worker = threading.Thread(
        target=client.post, args=("/invocations",),
        kwargs={"json": {"ticker": "MSFT", "question": "How liquid is it?"}},
    )
    worker.start()
    assert started.wait(5)
    assert client.get("/ping").json() == {"status": "HealthyBusy"}
    release.set()
    worker.join(5)
    assert client.get("/ping").json() == {"status": "Healthy"}


def test_a_bad_request_is_refused_before_anything_runs(runtime):
    client, s3 = runtime()
    assert client.post("/invocations", json={"ticker": "../etc", "question": "q?"}).status_code == 422
    assert client.post("/invocations", json={"ticker": "MSFT", "question": "How?",
                                             "caller": "Not A Name"}).status_code == 422
    assert not s3.objects


def test_the_daily_cap_is_checked_before_spending(runtime, monkeypatch):
    monkeypatch.setattr("fin_analyst.service.ESTIMATE_PER_MEMO", 5.0)
    client, s3 = runtime()
    reply = client.post("/invocations", json={"ticker": "MSFT", "question": "How liquid is it?"})
    assert reply.status_code == 429
    assert not s3.objects


def test_the_app_refuses_to_start_on_the_claude_api(monkeypatch):
    monkeypatch.setenv("FIN_ANALYST_PROVIDER", "anthropic")
    monkeypatch.setenv("JOBS_BUCKET", "b")
    with pytest.raises(RuntimeError, match="Bedrock"):
        build(s3=FakeS3())
