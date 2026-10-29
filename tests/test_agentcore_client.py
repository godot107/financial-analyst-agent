"""Calling the Runtime, and grading its answers with the gold set: fakes only, nothing spent."""

import io
import json

from fin_analyst.agentcore_client import as_state, invoke_memo

JOB = {
    "id": "20261029abc", "status": "done", "cost_usd": 0.05, "memo": "Net margin rose.",
    "result": {"metric_ids": ["current_ratio"], "drafts": 2, "trace": [{"seconds": 0.0}, {"seconds": 12.5}],
               "metrics": [{"fiscal_year": 2025, "value": 1.2}, {"fiscal_year": 2026, "value": None}]},
}


class FakeRuntime:
    def __init__(self):
        self.calls = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        return {"response": io.BytesIO(json.dumps(JOB).encode())}


def test_a_call_names_a_user_so_identity_can_hand_over_the_key():
    runtime = FakeRuntime()
    job = invoke_memo({"ticker": "MSFT", "question": "q"}, caller="gold", client=runtime,
                      target=("arn:aws:bedrock-agentcore:us-east-1:1:runtime/fin_analyst_memo-x", "live"))
    call = runtime.calls[0]
    assert call["runtimeUserId"] == "gold" and call["qualifier"] == "live"
    assert len(call["runtimeSessionId"]) >= 33
    assert json.loads(call["payload"]) == {"ticker": "MSFT", "question": "q", "caller": "gold"}
    assert job["id"] == "20261029abc"


def test_a_job_reads_like_the_graph_state_for_the_grader():
    state = as_state(JOB)
    assert state.memo == "Net margin rose." and state.metric_ids == ["current_ratio"]
    assert len(state.drafts) == 2
    assert [m.fiscal_year for m in state.metrics if m.value is not None] == [2025]
    failed = as_state({"status": "failed", "error": "gave up", "result": None})
    assert failed.memo is None and failed.error == "gave up" and failed.drafts == []


def test_the_gold_set_grades_runtime_answers_with_the_same_grader(tmp_path, monkeypatch, capsys):
    import evals.gold as gold

    monkeypatch.setattr(gold, "ROOT", tmp_path)
    case = {"ticker": "MSFT", "question": "How liquid?", "choose_any": [["current_ratio"]]}
    sent = []
    code = gold.run_on_runtime([case], max_usd=1.0, invoke=lambda request: sent.append(request) or JOB)
    assert sent == [{"ticker": "MSFT", "question": "How liquid?", "filings": 1}]
    out = capsys.readouterr().out
    assert "FAIL  first draft passed" in out and "2/3 checks passed" in out
    assert code == 1
    (record,) = json.loads(next(tmp_path.glob("runs/gold-runtime-*/gold.json")).read_text())
    assert record["job"] == "20261029abc" and record["cost_usd"] == 0.05
    assert record["overhead_seconds"] == round(record["seconds"] - 12.5, 2)


def test_a_session_can_be_reused_across_memos():
    runtime = FakeRuntime()
    target = ("arn", "live")
    for _ in range(2):
        invoke_memo({"ticker": "MSFT", "question": "q"}, caller="gold", client=runtime, target=target,
                    session="gold-0123456789abcdef0123456789abcdef")
    invoke_memo({"ticker": "MSFT", "question": "q"}, caller="gold", client=runtime, target=target)
    ids = [c["runtimeSessionId"] for c in runtime.calls]
    assert ids[0] == ids[1] and ids[2] != ids[0] and all(len(i) >= 33 for i in ids)


def test_overhead_is_the_wait_minus_the_graph():
    from fin_analyst.agentcore_client import graph_seconds

    assert graph_seconds(JOB) == 12.5
    assert graph_seconds({"status": "failed", "result": None}) is None
