"""Phase B's code, with fake AWS clients: nothing here touches AWS or spends anything."""

import io
import json

import pytest
from botocore.exceptions import ClientError

from fin_analyst.edgar import load_facts
from fin_analyst.jobs import JOB_ID, S3JobStore
from fin_analyst.lambda_handler import build, load_secrets
from tests.test_graph import CLEAN_DRAFT, FIXTURE, FakeAnalyst

FACTS = load_facts(FIXTURE)
KEY = "k" * 24


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]}


class FakeSSM:
    def __init__(self, values):
        self.values = values

    def get_parameters_by_path(self, Path, WithDecryption, Recursive, NextToken=None):
        # Two pages, to prove pagination is followed.
        items = [{"Name": f"{Path}/{k}", "Value": v} for k, v in self.values.items()]
        if NextToken is None:
            return {"Parameters": items[:1], "NextToken": "page-2"}
        return {"Parameters": items[1:]}


class FakeLambda:
    def __init__(self, fail=False):
        self.invocations = []
        self.fail = fail

    def invoke(self, FunctionName, InvocationType, Payload):
        if self.fail:
            raise RuntimeError("throttled")
        self.invocations.append((FunctionName, InvocationType, json.loads(Payload)))


def url_event(method, path, body=None, headers=None):
    """What a Lambda function URL delivers (payload format 2.0)."""
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"host": "abc.lambda-url.us-east-1.on.aws", "content-type": "application/json", **(headers or {})},
        "requestContext": {
            "http": {"method": method, "path": path, "protocol": "HTTP/1.1", "sourceIp": "10.0.0.1", "userAgent": "test"},
            "domainName": "abc.lambda-url.us-east-1.on.aws",
            "requestId": "req",
            "routeKey": "$default",
            "stage": "$default",
            "timeEpoch": 0,
        },
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.delenv("FIN_ANALYST_API_KEYS", raising=False)
    monkeypatch.setenv("JOBS_BUCKET", "fin-analyst-test")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "fin-analyst")

    def make(fail_dispatch=False):
        s3, lam = FakeS3(), FakeLambda(fail=fail_dispatch)
        analysts = []

        def analyst():
            analysts.append(FakeAnalyst([CLEAN_DRAFT]))
            return analysts[-1]

        handler, worker = build(
            ssm=FakeSSM({"FIN_ANALYST_API_KEYS": f"bot={KEY}", "SEC_USER_AGENT": "Test test@example.com"}),
            s3=s3,
            lambda_client=lam,
            analyst_factory=analyst,
            worker_kwargs={
                "fetch": lambda ticker: FACTS,
                "fetch_text": lambda ticker: [],
                "fetch_news": lambda ticker: [],
                "quote": None,
                "lookup_accession": lambda ticker: f"acc-{ticker}",
            },
        )
        return handler, worker, s3, lam, analysts

    return make


# --- the S3 job store ------------------------------------------------------


def test_a_job_round_trips_through_s3_under_its_date():
    s3 = FakeS3()
    store = S3JobStore("bucket", s3)
    job = store.add("memo", "bot", {"ticker": "MSFT"}, 0.06)

    assert JOB_ID.match(job.id)
    (key,) = s3.objects
    assert key.startswith("jobs/") and key.endswith(f"{job.id}.json")
    assert store.get(job.id).request == {"ticker": "MSFT"}


def test_today_s_spend_counts_estimates_until_jobs_finish():
    store = S3JobStore("bucket", FakeS3())
    first = store.add("memo", "bot", {}, 0.06)
    store.add("memo", "other", {}, 0.06)
    assert store.committed_today("bot") == pytest.approx(0.06)
    assert store.committed_today() == pytest.approx(0.12)

    store.claim(first.id)
    store.finish(first.id, "done", cost_usd=0.02, memo="m")
    assert store.committed_today("bot") == pytest.approx(0.02)


def test_an_id_that_is_not_an_id_never_reaches_s3():
    """The id becomes part of an object key; a path in it must go nowhere."""
    store = S3JobStore("bucket", FakeS3())
    assert store.get("../../secrets") is None
    assert store.get("20260916" + "z" * 24) is None


def test_a_job_is_claimed_only_once():
    store = S3JobStore("bucket", FakeS3())
    job = store.add("memo", "bot", {}, 0.06)
    assert store.claim(job.id).status == "running"
    assert store.claim(job.id) is None


# --- secrets ---------------------------------------------------------------


def test_secrets_are_read_across_pages_and_never_override_the_environment(monkeypatch):
    monkeypatch.delenv("FIN_ANALYST_API_KEYS", raising=False)
    monkeypatch.setenv("SEC_USER_AGENT", "Already Set a@example.com")

    load_secrets(FakeSSM({"SEC_USER_AGENT": "From SSM", "FIN_ANALYST_API_KEYS": f"bot={KEY}"}), "/fin-analyst")

    import os

    assert os.environ["SEC_USER_AGENT"] == "Already Set a@example.com"
    assert os.environ["FIN_ANALYST_API_KEYS"] == f"bot={KEY}"  # from the second page


# --- the handler ------------------------------------------------------------


def test_a_request_through_the_function_url_reaches_the_app(cloud):
    handler, *_ = cloud()
    response = handler(url_event("GET", "/v1/health"), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["status"] == "ok"


def test_a_submission_returns_at_once_and_starts_the_worker_asynchronously(cloud):
    handler, worker, s3, lam, analysts = cloud()
    response = handler(
        url_event("POST", "/v1/memos", {"ticker": "MSFT", "question": "How liquid is it?"}, {"x-api-key": KEY}),
        None,
    )

    assert response["statusCode"] == 202
    job_id = json.loads(response["body"])["id"]
    assert lam.invocations == [("fin-analyst", "Event", {"job_id": job_id})]
    assert analysts == [], "the request itself must not run the memo"

    finished = worker.process(job_id)
    assert finished.status == "done" and "1.23x" in finished.memo


def test_an_event_delivered_twice_does_not_pay_for_the_memo_twice(cloud):
    """Lambda's asynchronous delivery is at-least-once."""
    handler, worker, s3, lam, analysts = cloud()
    response = handler(
        url_event("POST", "/v1/memos", {"ticker": "MSFT", "question": "How liquid is it?"}, {"x-api-key": KEY}),
        None,
    )
    job_id = json.loads(response["body"])["id"]

    worker.process(job_id)
    worker.process(job_id)
    assert len(analysts) == 1


def test_a_job_that_cannot_be_started_is_marked_failed_not_left_queued(cloud):
    handler, worker, s3, lam, analysts = cloud(fail_dispatch=True)
    response = handler(
        url_event("POST", "/v1/memos", {"ticker": "MSFT", "question": "How liquid is it?"}, {"x-api-key": KEY}),
        None,
    )

    assert response["statusCode"] == 503
    (stored,) = [json.loads(body) for body in s3.objects.values()]
    assert stored["status"] == "failed" and "could not start" in stored["error"]
    assert stored["cost_usd"] == 0.0
