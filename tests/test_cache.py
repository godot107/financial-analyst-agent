"""The cache: filings by accession number, prices by day, memos by what shaped them.
Fakes throughout; the network guard in conftest.py would fail any real request."""

import io
import json
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from fin_analyst.cache import LocalCache, NoCache, S3Cache, code_version, memo_key
from fin_analyst.config import load_settings
from fin_analyst.edgar import fetch_facts, load_facts
from fin_analyst.jobs import JobStore
from fin_analyst.market import fetch_quote
from fin_analyst.news import fetch_press_releases
from fin_analyst.passages import fetch_passages
from fin_analyst.service import Worker
from tests.test_graph import CLEAN_DRAFT, FIXTURE, FakeAnalyst

FACTS = load_facts(FIXTURE)


# --- the stores -------------------------------------------------------------


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}


@pytest.fixture(params=["local", "s3"])
def cache(request, tmp_path):
    return LocalCache(tmp_path) if request.param == "local" else S3Cache("bucket", FakeS3())


def test_a_value_round_trips(cache):
    assert cache.get("filings/acc/facts.json") is None
    cache.put("filings/acc/facts.json", b"[1]")
    assert cache.get("filings/acc/facts.json") == b"[1]"


@pytest.mark.parametrize("key", ["../escape", "/etc/passwd", "filings/../../x", "has space", ""])
def test_a_key_that_could_escape_is_refused(cache, key):
    with pytest.raises(ValueError, match="unsafe cache key"):
        cache.put(key, b"x")


def test_s3_keys_live_under_the_cache_prefix():
    s3 = FakeS3()
    S3Cache("bucket", s3, prefix="cache/").put("prices/MSFT/2026-09-16.json", b"{}")
    assert list(s3.objects) == ["cache/prices/MSFT/2026-09-16.json"]


def test_no_cache_never_hits():
    off = NoCache()
    off.put("k", b"v")
    assert off.get("k") is None


# --- filings: fetched once per accession number ----------------------------


class FakeFiling:
    """Counts how often the expensive part - the XBRL or the report text - is read."""

    def __init__(self, accession="0001193125-26-323660"):
        self.accession_no = accession
        self.xbrl_reads = 0
        self.obj_reads = 0

    def xbrl(self):
        self.xbrl_reads += 1
        import pandas as pd

        rows = [
            {"concept": "us-gaap:Assets", "numeric_value": 100.0, "is_dimensioned": False,
             "period_type": "instant", "fiscal_period": None, "period_instant": "2026-06-30",
             "period_end": None, "fiscal_year": 2026},
        ]
        return SimpleNamespace(facts=SimpleNamespace(to_dataframe=lambda: pd.DataFrame(rows)))

    def obj(self):
        self.obj_reads += 1
        return SimpleNamespace(management_discussion="A" * 400, risk_factors="B" * 400)


def company_with(filing):
    return lambda ticker: SimpleNamespace(
        get_filings=lambda form: SimpleNamespace(latest=lambda: filing)
    )


def test_a_filing_s_facts_are_downloaded_once(cache, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")
    filing = FakeFiling()

    first = fetch_facts("MSFT", cache=cache, company=company_with(filing))
    second = fetch_facts("MSFT", cache=cache, company=company_with(filing))

    assert filing.xbrl_reads == 1
    assert first == second


def test_a_new_filing_is_never_missed_because_of_the_cache(cache, monkeypatch):
    """The latest-filing lookup runs every time; only the filing's content is cached."""
    monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")
    old, new = FakeFiling("0000000000-25-000001"), FakeFiling("0000000000-26-000002")

    fetch_facts("MSFT", cache=cache, company=company_with(old))
    facts = fetch_facts("MSFT", cache=cache, company=company_with(new))

    assert new.xbrl_reads == 1
    assert {f.accession for f in facts} == {"0000000000-26-000002"}


def test_the_report_text_is_read_once_per_filing(cache, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")
    filing = FakeFiling()
    fetch_passages("MSFT", cache=cache, company=company_with(filing))
    passages = fetch_passages("MSFT", cache=cache, company=company_with(filing))

    assert filing.obj_reads == 1
    assert [p.item for p in passages] == ["Item 7", "Item 1A"]


def test_press_releases_are_cached_each_but_renumbered_together(cache, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")
    reads = {"n": 0}

    def exhibit(text):
        def read():
            reads["n"] += 1
            return text
        return SimpleNamespace(document_type="EX-99.1", text=read)

    filings = [
        SimpleNamespace(accession_no="acc-1", filing_date="2026-09-02", attachments=[exhibit("A" * 300)]),
        SimpleNamespace(accession_no="acc-2", filing_date="2026-07-29", attachments=[exhibit("B" * 300)]),
    ]
    company = lambda ticker: SimpleNamespace(
        get_filings=lambda form: SimpleNamespace(head=lambda limit: filings)
    )

    fetch_press_releases("MSFT", cache=cache, company=company)
    again = fetch_press_releases("MSFT", start=5, cache=cache, company=company)

    assert reads["n"] == 2  # each release read once, not twice
    assert [p.id for p in again] == ["N5", "N6"]  # ids follow this call, not the cache
    assert again[0].published == "2026-09-02"


# --- prices: once per day ---------------------------------------------------


def test_a_price_is_fetched_once_a_day(cache):
    calls = {"n": 0}

    def opener(url, timeout=None):
        calls["n"] += 1
        return io.BytesIO(json.dumps({"Global Quote": {"05. price": "500.0", "07. latest trading day": "2026-09-15"}}).encode())

    first = fetch_quote("MSFT", api_key="demo", opener=opener, cache=cache)
    second = fetch_quote("MSFT", api_key="demo", opener=opener, cache=cache)

    assert calls["n"] == 1  # the free plan allows 25 requests a day
    assert first == second


# --- memos: reused only when nothing that shapes them has changed ------------


def test_the_memo_key_changes_with_everything_that_shapes_a_memo():
    base = memo_key({"ticker": "MSFT", "question": "How liquid is it?"}, ["acc-1"], "v1")

    # Only spacing and case differ: the same question.
    assert memo_key({"ticker": "MSFT", "question": "  how LIQUID is it? "}, ["acc-1"], "v1") == base
    # A new filing, new code, a different question, different options, another day.
    assert memo_key({"ticker": "MSFT", "question": "How liquid is it?"}, ["acc-2"], "v1") != base
    assert memo_key({"ticker": "MSFT", "question": "How liquid is it?"}, ["acc-1"], "v2") != base
    assert memo_key({"ticker": "MSFT", "question": "How profitable is it?"}, ["acc-1"], "v1") != base
    assert memo_key({"ticker": "MSFT", "question": "How liquid is it?", "verify": False}, ["acc-1"], "v1") != base
    assert memo_key({"ticker": "MSFT", "question": "How liquid is it?"}, ["acc-1"], "v1", day="2026-09-16") != base


def test_the_code_version_is_a_fingerprint_of_the_memo_writing_code(tmp_path):
    package = tmp_path / "fin_analyst"
    package.mkdir()
    from fin_analyst.cache import MEMO_SOURCES

    for name in MEMO_SOURCES:
        (package / name).write_text("original")
    before = code_version(package)
    (package / "llm.py").write_text("a changed prompt")
    assert code_version(package) != before


class Setup:
    def __init__(self, tmp_path, accession="acc-1"):
        self.store = JobStore(tmp_path / "jobs.sqlite3")
        self.cache = LocalCache(tmp_path / "cache")
        self.analysts = []
        self.accession = accession
        self.worker = Worker(
            self.store,
            load_settings(),
            analyst_factory=self._analyst,
            runs_dir=tmp_path / "runs",
            fetch=lambda ticker: FACTS,
            fetch_text=lambda ticker: [],
            fetch_news=lambda ticker: [],
            quote=None,
            cache=self.cache,
            lookup_accession=lambda ticker: self.accession,
        )

    def _analyst(self):
        self.analysts.append(FakeAnalyst([CLEAN_DRAFT]))
        return self.analysts[-1]

    def run(self, **request):
        body = {"ticker": "MSFT", "question": "How liquid is it?", **request}
        job = self.store.add("memo", "bot", body, 0.06)
        self.worker.process(job.id)
        return self.store.get(job.id)


def test_asking_again_with_reuse_costs_nothing(tmp_path):
    setup = Setup(tmp_path)
    first = setup.run()
    second = setup.run(reuse=True)

    assert len(setup.analysts) == 1, "a cached memo should not even build an analyst"
    assert second.status == "done" and second.cost_usd == 0.0
    assert second.memo == first.memo
    assert second.result["reused_from"] == first.id


def test_without_reuse_a_fresh_memo_is_written(tmp_path):
    setup = Setup(tmp_path)
    setup.run()
    setup.run()  # reuse defaults to off
    assert len(setup.analysts) == 2


def test_a_new_filing_means_a_new_memo_even_with_reuse(tmp_path):
    setup = Setup(tmp_path)
    setup.run()
    setup.accession = "acc-2"  # the company filed a new 10-K
    fresh = setup.run(reuse=True)

    assert len(setup.analysts) == 2
    assert "reused_from" not in fresh.result


def test_a_failed_memo_is_never_reused(tmp_path):
    setup = Setup(tmp_path)
    clean = setup.worker.analyst_factory
    setup.worker.analyst_factory = lambda: FakeAnalyst(["The ratio is 1.23."] * 3)  # fails closed
    assert setup.run().status == "failed"

    setup.worker.analyst_factory = clean
    retried = setup.run(reuse=True)
    assert retried.status == "done"
    assert "reused_from" not in retried.result


def test_if_the_filing_cannot_be_identified_nothing_is_reused(tmp_path):
    """Without an accession number there is no safe key, so write a fresh memo."""
    setup = Setup(tmp_path)
    setup.run()

    def unreachable(ticker):
        raise RuntimeError("EDGAR is down")

    setup.worker.lookup_accession = unreachable
    setup.run(reuse=True)
    assert len(setup.analysts) == 2
