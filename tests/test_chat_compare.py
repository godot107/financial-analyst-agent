"""The chat comparison's free parts: the question set, blinding, the sheet and the score."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

from chat_compare import (  # noqa: E402
    ARMS, blind, estimate, grades_template, grading_sheet, load_cases, main, score,
)
from fin_analyst.metrics import METRICS_BY_ID  # noqa: E402


def row(case_id: str, pipeline: dict, strands: dict) -> dict:
    base = {"answer": "x", "error": None, "drafts": 1, "first_draft_pass": True, "claims": 0,
            "claims_supported": 0, "cost_usd": 0.05, "seconds": 1, "model_calls": 2, "tool_calls": 0}
    return {"id": case_id, "ticker": "MSFT", "opening": "o", "question": "q", "expect": "e",
            "pipeline": {**base, **pipeline}, "strands": {**base, **strands}}


def test_the_question_set_is_twelve_follow_ups_over_three_companies():
    plan, cases = load_cases()
    assert len(cases) == 12 and {c["ticker"] for c in cases} == {"MSFT", "COST", "JPM"}
    assert len({c["id"] for c in cases}) == 12
    assert {c["kind"] for c in cases} == {"narrow", "open", "off_plan", "trap"}
    assert all(c["expect"] for c in cases)
    assert "2x" in plan["decision_rule"]


def test_the_expect_lines_name_metrics_that_exist():
    # A renamed metric would make a grading line describe something neither arm can return.
    assert {"quick_ratio", "loans_to_deposits", "credit_cost_to_loans", "current_ratio"} <= set(METRICS_BY_ID)


def test_the_default_run_spends_nothing_and_states_the_estimate(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    _, cases = load_cases()
    assert f"about ${estimate(cases):.2f}" in out and "--spend" in out


def test_blinding_is_shuffled_but_reproducible():
    results = [row(f"c{i}", {}, {}) for i in range(12)]
    first = [r["key"]["A"] for r in blind(results, 7)]
    again = [r["key"]["A"] for r in blind([row(f"c{i}", {}, {}) for i in range(12)], 7)]
    assert first == again and set(first) == set(ARMS)


def test_the_grading_sheet_never_names_an_arm():
    results = blind([row("c1", {"answer": "pipeline says"}, {"answer": "agent says"})], 1)
    sheet = grading_sheet(results, {"grading": "0 1 2"})
    assert "pipeline says" in sheet and "agent says" in sheet
    assert "pipeline" not in sheet.replace("pipeline says", "")
    assert "strands" not in sheet.lower()
    assert grades_template(results) == "c1:\n  A:\n  B:\n"


def test_score_unblinds_and_applies_the_decision_rule():
    results = blind([
        row("c1", {"cost_usd": 0.05}, {"cost_usd": 0.08}),
        row("c2", {"cost_usd": 0.05}, {"cost_usd": 0.08, "drafts": 2, "first_draft_pass": False}),
    ], 3)
    grades = {}
    for r in results:  # strands 2, pipeline 1, whichever letter each got
        grades[r["id"]] = {label: (2 if arm == "strands" else 1) for label, arm in r["key"].items()}
    summary = score(results, grades)

    assert summary["strands"]["mean_grade"] == 2 and summary["pipeline"]["mean_grade"] == 1
    assert summary["verdict"]["cost_within_2x"]  # $0.08 vs $0.05
    assert not summary["verdict"]["first_draft_no_lower"]  # 50% vs 100%
    assert not summary["verdict"]["strands_earns_a_place"]


def test_a_missing_grade_is_an_error_not_a_zero():
    results = blind([row("c1", {}, {})], 1)
    with pytest.raises(ValueError, match="c1"):
        score(results, {"c1": {"A": 2}})


def test_the_paid_path_runs_end_to_end_on_fakes(monkeypatch, tmp_path):
    """--spend's wiring, with every model and every fetch faked, so a bug can't cost a run."""
    pytest.importorskip("strands")
    import chat_compare
    from fin_analyst.edgar import load_facts
    from fin_analyst.passages import load_passages
    from fin_analyst.strands_chat import build_model as real_build_model
    from tests.fake_stream import ScriptedClaude, text, tool_call
    from tests.test_graph import CLEAN_DRAFT, FIXTURE, PASSAGES_FIXTURE, FakeAnalyst

    class SpendingFake(FakeAnalyst):
        def __init__(self, settings):
            super().__init__([CLEAN_DRAFT] * 3)
            self.spent_usd = 0.0

        def choose_metrics(self, *args):
            self.spent_usd += self.cost
            return super().choose_metrics(*args)

        def write_draft(self, *args, **kwargs):
            self.spent_usd += self.cost
            return super().write_draft(*args, **kwargs)

    facts, passages = load_facts(FIXTURE), load_passages(PASSAGES_FIXTURE)
    answer = "The current ratio {{current_ratio:2025->2026}}."
    script = ScriptedClaude([tool_call("t", "get_metrics", ids=["current_ratio"]), text(answer)] * 4)

    def no_filing(ticker, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr("fin_analyst.llm.ClaudeAnalyst", SpendingFake)
    monkeypatch.setattr("fin_analyst.edgar.fetch_facts", lambda ticker, **kw: facts)
    monkeypatch.setattr("fin_analyst.edgar.describe_filing", no_filing)
    monkeypatch.setattr("fin_analyst.passages.fetch_passages", lambda ticker, **kw: passages)
    monkeypatch.setattr("fin_analyst.cache.LocalCache", lambda path: None)
    monkeypatch.setattr(
        "fin_analyst.strands_chat.build_model",
        lambda settings: real_build_model(settings, {"api_key": "fake", "http_client": script.client()}),
    )

    plan, cases = load_cases()
    msft = [c for c in cases if c["ticker"] == "MSFT"]
    results = chat_compare.run(msft, plan, max_usd=3.0, out=tmp_path)

    assert len(results) == 4
    for r in results:
        assert r["pipeline"]["answer"] and r["strands"]["answer"], r
        assert r["strands"]["tools"] == ["get_metrics"]
        assert r["strands"]["cost_usd"] == pytest.approx(0.02)  # two scripted calls
        assert r["pipeline"]["cost_usd"] == pytest.approx(0.02)  # plan + write
    assert {p.name for p in tmp_path.iterdir()} >= {"results.json", "grading.md", "grades.yaml"}
