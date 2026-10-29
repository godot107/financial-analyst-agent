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

    def make(analyst=None, fetch_key=None):
        s3 = FakeS3()
        app = build(
            s3=s3,
            fetch_key=fetch_key,
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


# --- the Claude API: the key comes from AgentCore Identity ------------------------

MEMO = {"ticker": "MSFT", "question": "How liquid is it?", "text": False, "verify": False}


def no_key_in_env(monkeypatch):
    """Like the container: no key until Identity hands one over. Called after build(),
    because load_settings() reads the project's .env. setenv first, so teardown
    removes whatever the app sets and nothing leaks into other tests."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_API_KEY")


@pytest.fixture
def claude_api(runtime, monkeypatch):
    monkeypatch.setenv("FIN_ANALYST_PROVIDER", "anthropic")
    tokens = []

    def fetch(token):
        tokens.append(token)
        return "sk-from-identity"

    client, s3 = runtime(fetch_key=fetch)
    no_key_in_env(monkeypatch)
    return client, s3, tokens


def test_the_key_is_fetched_once_with_the_runtime_s_token(claude_api):
    import os

    client, _, tokens = claude_api
    for _ in range(2):
        reply = client.post("/invocations", json=MEMO, headers={"WorkloadAccessToken": "wat-1"})
        assert reply.json()["status"] == "done"
    assert tokens == ["wat-1"]  # once per container, not per memo
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-from-identity"


def test_the_identity_header_name_is_read_first(claude_api):
    client, _, tokens = claude_api
    client.post("/invocations", json=MEMO,
                headers={"X-Amz-Bedrock-AgentCore-Identity-WAT": "wat-a", "WorkloadAccessToken": "wat-b"})
    assert tokens == ["wat-a"]


def test_no_token_means_no_memo_and_nothing_spent(claude_api):
    client, s3, tokens = claude_api
    reply = client.post("/invocations", json=MEMO)
    assert reply.status_code == 401 and "runtimeUserId" in reply.json()["detail"]
    assert not tokens and not s3.objects


def test_an_identity_failure_names_the_cause_never_a_key(runtime, monkeypatch):
    monkeypatch.setenv("FIN_ANALYST_PROVIDER", "anthropic")

    def refuse(token):
        raise PermissionError("sk-should-never-show")

    client, s3 = runtime(fetch_key=refuse)
    no_key_in_env(monkeypatch)
    reply = client.post("/invocations", json=MEMO, headers={"WorkloadAccessToken": "t"})
    assert reply.status_code == 401
    assert "PermissionError" in reply.json()["detail"] and "sk-" not in reply.json()["detail"]
    assert not s3.objects


def test_identity_fetcher_calls_get_resource_api_key():
    from fin_analyst.agentcore_app import identity_key_fetcher

    calls = []

    class FakeIdentity:
        def get_resource_api_key(self, **kwargs):
            calls.append(kwargs)
            return {"apiKey": "k"}

    fetch = identity_key_fetcher("fin-analyst-anthropic", "us-east-1", client=FakeIdentity())
    assert fetch("tok") == "k"
    assert calls == [{"resourceCredentialProviderName": "fin-analyst-anthropic", "workloadIdentityToken": "tok"}]


def test_bedrock_needs_no_key_and_no_token(runtime):
    client, _ = runtime()
    assert client.post("/invocations", json=MEMO).json()["status"] == "done"
