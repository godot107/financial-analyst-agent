"""The Strands chat, driven by a scripted Claude. Free: no network, no key.

Skipped when strands-agents isn't installed (requirements-chat.txt), so the main
suite never depends on it.
"""

import dataclasses
import json

import pytest

pytest.importorskip("strands")

from strands.models.anthropic import AnthropicModel  # noqa: E402

from fin_analyst.chat_tools import ChatToolbox  # noqa: E402
from fin_analyst.config import load_settings  # noqa: E402
from fin_analyst.edgar import load_facts  # noqa: E402
from fin_analyst.llm import FALLBACK_BETA  # noqa: E402
from fin_analyst.passages import load_passages  # noqa: E402
from fin_analyst.strands_chat import StrandsChatSession, agent_tools, build_model  # noqa: E402
from tests.fake_stream import ScriptedClaude, text, tool_call  # noqa: E402
from tests.test_graph import FIXTURE, PASSAGES_FIXTURE, FakeVerdict  # noqa: E402

# 1000 input + 200 output tokens per scripted call is $0.01 at Opus 5 prices.
CALL_USD = 0.01
CLEAN = "The current ratio {{current_ratio:2025->2026}}."
CITED = "The current ratio {{current_ratio:2025->2026}}, as cash went to buybacks [P50]."


@pytest.fixture
def toolbox():
    return ChatToolbox(
        "msft", load_facts(FIXTURE), load_passages(PASSAGES_FIXTURE), company="MICROSOFT CORP"
    )


@pytest.fixture
def settings():
    return load_settings()


def session_for(script, toolbox, settings, tmp_path, verifier=None, **overrides):
    settings = dataclasses.replace(settings, **overrides)
    model = build_model(settings, client_args={"api_key": "fake", "http_client": script.client()})
    return StrandsChatSession(toolbox, settings, verifier=verifier, model=model, runs_dir=tmp_path)


class FakeVerifier:
    def __init__(self, answers: list[bool]):
        self.answers = answers
        self.asked = []

    def verify_claims(self, claims):
        self.asked.append(claims)
        supported = self.answers.pop(0)
        return [FakeVerdict(supported, "the passage is about buybacks") for _ in claims], 0.002


# --- the tools, as Strands sees them ----------------------------------------


def test_only_the_tools_this_conversation_can_use_are_offered(toolbox):
    assert [t.tool_name for t in agent_tools(toolbox)] == [
        "list_metrics", "get_metrics", "search_filing",
    ]
    toolbox.fetch_peer = toolbox.fetch_news = lambda ticker: []
    assert [t.tool_name for t in agent_tools(toolbox)][-2:] == ["compare_with", "recent_news"]


def test_the_model_sees_the_docstrings_and_arguments_not_self(toolbox):
    specs = {t.tool_name: t.tool_spec for t in agent_tools(toolbox)}
    schema = specs["get_metrics"]["inputSchema"]["json"]
    assert schema["required"] == ["ids"]
    assert "self" not in schema["properties"]
    assert "placeholder" in specs["get_metrics"]["description"]


# --- the model: config.yaml's settings, the Claude API, nothing else --------


def test_the_request_carries_config_yaml_settings(toolbox, settings, tmp_path):
    script = ScriptedClaude([text(CLEAN)])
    chat = session_for(script, toolbox, settings, tmp_path)
    assert isinstance(chat.agent.model, AnthropicModel)  # the Claude API, never Bedrock
    toolbox.get_metrics(["current_ratio"])
    chat.ask("How liquid is it?")

    sent = script.requests[0]
    node = settings.nodes["chat_agent"]
    assert sent["model"] == node.model and sent["max_tokens"] == node.max_tokens
    assert sent["output_config"] == {"effort": node.effort}
    assert sent["thinking"]["type"] == "adaptive"
    assert sent["fallbacks"] == "default"
    assert FALLBACK_BETA in script.headers[0]["anthropic-beta"]
    assert "never type one" in sent["system"][0]["text"]


# --- one question --------------------------------------------------------------


def test_a_clean_answer_is_rendered_costed_and_saved(toolbox, settings, tmp_path):
    script = ScriptedClaude([
        tool_call("t1", "get_metrics", ids=["current_ratio"]),
        tool_call("t2", "search_filing", query="liquidity cash"),
        text(CLEAN),
    ])
    answer = session_for(script, toolbox, settings, tmp_path).ask("How liquid is it?")

    assert answer.error is None and answer.drafts == [CLEAN]
    assert "{{" not in answer.answer and "---" in answer.answer  # rendered, with the footer
    assert [c["tool"] for c in answer.tool_calls] == ["get_metrics", "search_filing"]
    assert answer.cost_usd == pytest.approx(3 * CALL_USD)
    record = json.loads(next(tmp_path.glob("MSFT-chat-*.json")).read_text())
    assert record["mode"] == "strands" and record["answer"] == answer.answer
    assert {e["step"] for e in record["trace"]} >= {"run", "claude", "tool", "check"}


def test_a_leaked_digit_is_sent_back_and_fixed(toolbox, settings, tmp_path):
    script = ScriptedClaude([
        tool_call("t1", "get_metrics", ids=["current_ratio"]),
        text("The current ratio fell to 1.23x."),
        text(CLEAN),
    ])
    answer = session_for(script, toolbox, settings, tmp_path).ask("How liquid is it?")

    assert answer.answer and len(answer.drafts) == 2
    retry = script.requests[2]["messages"][-1]["content"][0]["text"]
    assert "rejected" in retry and "number you wrote yourself" in retry


def test_it_gives_up_after_max_retries(toolbox, settings, tmp_path):
    leaky = text("The current ratio fell to 1.23x.")
    script = ScriptedClaude([tool_call("t1", "get_metrics", ids=["current_ratio"])] + [leaky] * 2)
    answer = session_for(script, toolbox, settings, tmp_path, max_retries=1).ask("How liquid?")

    assert answer.answer is None and "gave up after 2 drafts" in answer.error
    assert next(tmp_path.glob("MSFT-chat-*.json"))  # a failure is recorded too
    assert not list(tmp_path.glob("*.md"))


def test_an_unsupported_citation_is_sent_back(toolbox, settings, tmp_path):
    script = ScriptedClaude([
        tool_call("t1", "get_metrics", ids=["current_ratio"]),
        tool_call("t2", "search_filing", query="liquidity cash"),
        text(CITED),
        text(CLEAN),
    ])
    verifier = FakeVerifier([False])
    answer = session_for(script, toolbox, settings, tmp_path, verifier).ask("Why?")

    assert answer.answer and len(answer.drafts) == 2
    assert [c.supported for c in answer.claim_checks] == [False]
    assert "not supported" in script.requests[3]["messages"][-1]["content"][0]["text"]
    assert len(verifier.asked) == 1  # the clean second draft cites nothing
    assert answer.cost_usd == pytest.approx(4 * CALL_USD + 0.002)


# --- the limits --------------------------------------------------------------


def test_tool_calls_past_the_cap_are_cancelled(toolbox, settings, tmp_path):
    script = ScriptedClaude([
        tool_call("t1", "get_metrics", ids=["current_ratio"]),
        tool_call("t2", "search_filing", query="liquidity"),
        text(CLEAN),
    ])
    answer = session_for(script, toolbox, settings, tmp_path, chat_max_tool_calls=1).ask("How?")

    assert [c["cancelled"] for c in answer.tool_calls] == [False, True]
    assert "No more tool calls" in json.dumps(script.requests[2]["messages"][-1])
    assert toolbox.shown_passages == {}  # the cancelled search never ran
    assert answer.answer


def test_the_spend_guard_stops_the_next_model_call(toolbox, settings, tmp_path):
    script = ScriptedClaude([
        tool_call("t1", "get_metrics", ids=["current_ratio"]),
        tool_call("t2", "search_filing", query="liquidity"),
        text(CLEAN),
    ])
    answer = session_for(script, toolbox, settings, tmp_path, max_usd_per_run=0.015).ask("How?")

    assert len(script.requests) == 2  # $0.02 spent after two calls; the third never went out
    assert answer.answer is None and "over the $0.01 limit" in answer.error


# --- the conversation ------------------------------------------------------------


def test_follow_ups_keep_the_conversation_and_the_turn_cap(toolbox, settings, tmp_path):
    script = ScriptedClaude([
        tool_call("t1", "get_metrics", ids=["current_ratio"]),
        text(CLEAN),
        text("The current ratio was {{current_ratio:2026}}."),  # reuses what turn 1 fetched
    ])
    chat = session_for(script, toolbox, settings, tmp_path, chat_max_turns=2)
    first = chat.ask("How liquid is it?")
    second = chat.ask("And the level?")

    assert first.answer and second.answer and second.tool_calls == []
    assert "How liquid is it?" in json.dumps(script.requests[2]["messages"])
    assert second.session_cost_usd == pytest.approx(3 * CALL_USD)
    with pytest.raises(RuntimeError, match="used its 2 turns"):
        chat.ask("One more?")
