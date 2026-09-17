"""Phase A: the HTTP service. The worker is driven by hand with the fake analyst,
so nothing here reaches the network or spends anything."""

import dataclasses

import pytest
from fastapi.testclient import TestClient

from fin_analyst.config import load_settings
from fin_analyst.edgar import load_facts
from fin_analyst.jobs import JobStore
from fin_analyst.server import parse_keys
from fin_analyst.service import Worker, create_app
from fin_analyst.passages import load_passages, search
from tests.test_graph import (
    CLEAN_DRAFT, FIXTURE, LEAKY_DRAFT, PASSAGES_FIXTURE, FakeAnalyst, msft_filing,
)

FACTS = load_facts(FIXTURE)
BOT = {"X-API-Key": "b" * 24}
WILLIE = {"X-API-Key": "w" * 24}
MEMO = {"ticker": "MSFT", "question": "How liquid is it?"}


class Service:
    """An app, its store and its worker, with a record of every analyst built."""

    def __init__(
        self, tmp_path, drafts=(CLEAN_DRAFT,), per_key=1.0, global_cap=3.0, fetch=None,
        describe=None, fetch_text=None,
    ):
        self.settings = dataclasses.replace(
            load_settings(), api_per_key_daily_usd=per_key, api_global_daily_usd=global_cap
        )
        self.store = JobStore(tmp_path / "jobs.sqlite3")
        self.drafts = list(drafts)
        self.analysts: list[FakeAnalyst] = []
        fetch = fetch or (lambda ticker: FACTS)
        self.worker = Worker(
            self.store,
            self.settings,
            analyst_factory=self._analyst,
            runs_dir=tmp_path / "runs",
            fetch=fetch,
            fetch_text=fetch_text or (lambda ticker: []),
            fetch_news=lambda ticker: [],
            quote=None,
            describe=describe,
        )
        app = create_app(
            self.store, self.settings, {"bot": "b" * 24, "willie": "w" * 24}, self.worker,
            fetch=fetch,
        )
        self.client = TestClient(app)

    def _analyst(self):
        analyst = FakeAnalyst(self.drafts)
        self.analysts.append(analyst)
        return analyst


@pytest.fixture
def service(tmp_path):
    return Service(tmp_path)


# --- the job lifecycle ------------------------------------------------------


def test_a_memo_is_accepted_at_once_and_collected_later(service):
    response = service.client.post("/v1/memos", json=MEMO, headers=BOT)

    assert response.status_code == 202
    accepted = response.json()
    assert accepted["estimate_usd"] > 0
    assert service.client.get(accepted["status_url"], headers=BOT).json()["status"] == "queued"
    assert service.analysts == [], "nothing may run until the worker picks the job up"

    service.worker.process_next()

    job = service.client.get(accepted["status_url"], headers=BOT).json()
    assert job["status"] == "done"
    assert "fell 0.12x to 1.23x" in job["memo"]
    assert job["cost_usd"] > 0
    assert job["result"]["metric_ids"] == ["current_ratio"]
    assert [e["step"] for e in job["result"]["trace"]][:2] == ["run", "plan"]


def test_the_result_carries_the_filing_metrics_and_passages_as_data(tmp_path):
    """A program reads the numbers from here instead of parsing the memo."""
    passages = load_passages(PASSAGES_FIXTURE)
    cited = search(passages, "How liquid is it?", 4)[0].id
    service = Service(
        tmp_path,
        drafts=[f"Cash fell [{cited}]: the current ratio {{{{current_ratio:2025->2026}}}}."],
        describe=lambda ticker: msft_filing(),
        fetch_text=lambda ticker: passages,
    )
    job_id = service.client.post("/v1/memos", json=MEMO, headers=BOT).json()["id"]
    service.worker.process_next()
    result = service.client.get(f"/v1/memos/{job_id}", headers=BOT).json()["result"]

    assert result["filing"]["accession"] == "0001193125-26-323660"
    assert result["filing"]["period"] == "2026-06-30"
    latest = next(m for m in result["metrics"] if m["fiscal_year"] == 2026)
    assert latest["metric_id"] == "current_ratio" and latest["formatted"] == "1.23x"
    assert latest["inputs"]["current_assets"] > 0
    assert [p["id"] for p in result["passages"] if p["cited"]] == [cited]
    assert "facts" not in result, "facts only when asked for"


def test_facts_are_included_when_asked_for(service):
    job_id = service.client.post(
        "/v1/memos", json={**MEMO, "include_facts": True}, headers=BOT
    ).json()["id"]
    service.worker.process_next()
    result = service.client.get(f"/v1/memos/{job_id}", headers=BOT).json()["result"]

    assert len(result["facts"]) == len(FACTS)
    assert {"line_item", "concept", "period", "accession"} <= set(result["facts"][0])


def test_more_filings_are_asked_for_only_when_requested(tmp_path):
    asked = []

    def fetch(ticker, filings=1):
        asked.append(filings)
        return FACTS

    service = Service(tmp_path, drafts=[CLEAN_DRAFT] * 2, fetch=fetch)
    for body in (MEMO, {**MEMO, "filings": 3}):
        service.client.post("/v1/memos", json=body, headers=BOT)
        service.worker.process_next()

    assert asked == [1, 3]
    assert service.client.post("/v1/memos", json={**MEMO, "filings": 6}, headers=BOT).status_code == 422


def test_a_memo_that_fails_closed_is_a_result_not_a_server_error(tmp_path):
    service = Service(tmp_path, drafts=[LEAKY_DRAFT] * 3)
    job_id = service.client.post("/v1/memos", json=MEMO, headers=BOT).json()["id"]
    service.worker.process_next()

    response = service.client.get(f"/v1/memos/{job_id}", headers=BOT)
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert "gave up after 3 drafts" in response.json()["error"]


def test_one_broken_job_does_not_stop_the_worker(tmp_path):
    calls = {"n": 0}

    def fetch(ticker):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("no facts found in the latest 10-K for NOPE")
        return FACTS

    service = Service(tmp_path, fetch=fetch)
    first = service.client.post("/v1/memos", json={**MEMO, "ticker": "NOPE"}, headers=BOT).json()["id"]
    second = service.client.post("/v1/memos", json=MEMO, headers=BOT).json()["id"]

    service.worker.process_next()
    service.worker.process_next()

    assert service.store.get(first).status == "failed"
    assert "no facts found" in service.store.get(first).error
    assert service.store.get(second).status == "done"


def test_a_batch_runs_as_one_job(service):
    service.drafts = [CLEAN_DRAFT] * 2
    body = {"tickers": ["MSFT", "COST"], "question": "How liquid is it?", "max_usd": 0.5}
    job_id = service.client.post("/v1/batches", json=body, headers=BOT).json()["id"]
    service.worker.process_next()

    job = service.store.get(job_id)
    assert job.status == "done"
    assert [item["ticker"] for item in job.result["items"]] == ["MSFT", "COST"]


# --- keys --------------------------------------------------------------------


def test_every_route_but_health_needs_a_key(service):
    assert service.client.get("/v1/health").status_code == 200
    assert service.client.post("/v1/memos", json=MEMO).status_code == 401
    assert service.client.post("/v1/memos", json=MEMO, headers={"X-API-Key": "guess"}).status_code == 401
    assert service.client.get("/v1/coverage/MSFT").status_code == 401


def test_a_caller_cannot_read_another_caller_s_job(service):
    job_id = service.client.post("/v1/memos", json=MEMO, headers=BOT).json()["id"]
    # Reported as missing rather than forbidden: an id alone confirms nothing.
    assert service.client.get(f"/v1/memos/{job_id}", headers=WILLIE).status_code == 404


def test_the_service_refuses_to_start_open(tmp_path):
    service = Service(tmp_path)
    with pytest.raises(ValueError, match="without at least one API key"):
        create_app(service.store, service.settings, {}, service.worker)


def test_keys_are_read_from_the_environment_format():
    assert parse_keys("bot=" + "b" * 20 + ", willie=" + "w" * 20) == {"bot": "b" * 20, "willie": "w" * 20}
    with pytest.raises(ValueError, match="16"):
        parse_keys("bot=short")


# --- spending caps -----------------------------------------------------------


def test_a_job_over_the_key_s_cap_is_refused_before_any_claude_call(tmp_path):
    service = Service(tmp_path, per_key=0.10)

    assert service.client.post("/v1/memos", json=MEMO, headers=BOT).status_code == 202
    refused = service.client.post("/v1/memos", json=MEMO, headers=BOT)

    assert refused.status_code == 429
    detail = refused.json()["detail"]
    assert "this key's daily spending cap" in detail["error"]
    assert "resets_at" in detail
    # The first job is still only queued: the refusal counted its estimate, and
    # nothing has called the analyst at all.
    assert service.analysts == []


def test_the_global_cap_covers_every_key_together(tmp_path):
    service = Service(tmp_path, per_key=1.0, global_cap=0.10)

    assert service.client.post("/v1/memos", json=MEMO, headers=BOT).status_code == 202
    refused = service.client.post("/v1/memos", json=MEMO, headers=WILLIE)

    assert refused.status_code == 429
    assert "the service's daily spending cap" in refused.json()["detail"]["error"]


def test_a_finished_job_counts_at_its_real_cost_not_its_estimate(tmp_path):
    """A cheap memo should free up room, or the cap would bite far too early."""
    service = Service(tmp_path, per_key=0.10)
    service.client.post("/v1/memos", json=MEMO, headers=BOT)
    service.worker.process_next()  # the fake costs $0.02, against a $0.06 estimate

    assert service.store.committed_today("bot") == pytest.approx(0.02)
    assert service.client.post("/v1/memos", json=MEMO, headers=BOT).status_code == 202


def test_a_ratios_only_memo_is_estimated_cheaper(service):
    cheap = {**MEMO, "text": False, "verify": False}
    full = service.client.post("/v1/memos", json=MEMO, headers=BOT).json()["estimate_usd"]
    ratios = service.client.post("/v1/memos", json=cheap, headers=BOT).json()["estimate_usd"]
    assert ratios < full


def test_health_reports_what_has_been_committed_today(service):
    service.client.post("/v1/memos", json=MEMO, headers=BOT)
    health = service.client.get("/v1/health").json()
    assert health["spent_today_usd"] > 0
    assert health["global_daily_cap_usd"] == service.settings.api_global_daily_usd


# --- validation and the free route -------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {**MEMO, "ticker": "MSFT; DROP TABLE jobs"},
        {**MEMO, "question": ""},
        {"ticker": "MSFT"},
    ],
)
def test_malformed_memo_requests_are_rejected(service, body):
    assert service.client.post("/v1/memos", json=body, headers=BOT).status_code == 422


def test_a_batch_must_say_what_it_may_spend(service):
    body = {"tickers": ["MSFT"], "question": "How liquid is it?"}
    assert service.client.post("/v1/batches", json=body, headers=BOT).status_code == 422


def test_coverage_is_answered_straight_away_and_calls_no_model(service):
    response = service.client.get("/v1/coverage/msft", headers=BOT)

    assert response.status_code == 200
    body = response.json()
    assert body["ticker"] == "MSFT"
    assert any("short_term_borrowings" in note for note in body["concerns"])
    assert service.analysts == []
