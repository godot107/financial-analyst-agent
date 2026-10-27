"""The toolbox as Strands tools, driven by a scripted Claude. Free: no network, no key.

Skipped when strands-agents isn't installed (requirements-chat.txt), so the main
suite never depends on it.
"""

import pytest

pytest.importorskip("strands")

from strands import Agent  # noqa: E402
from strands.models.anthropic import AnthropicModel  # noqa: E402

from fin_analyst.chat_tools import ChatToolbox  # noqa: E402
from fin_analyst.edgar import load_facts  # noqa: E402
from fin_analyst.passages import load_passages  # noqa: E402
from fin_analyst.strands_chat import agent_tools  # noqa: E402
from tests.fake_stream import ScriptedClaude, text, tool_call  # noqa: E402
from tests.test_graph import FIXTURE, PASSAGES_FIXTURE  # noqa: E402


@pytest.fixture
def toolbox():
    return ChatToolbox(
        "msft", load_facts(FIXTURE), load_passages(PASSAGES_FIXTURE), company="MICROSOFT CORP"
    )


def agent_for(script: ScriptedClaude, toolbox: ChatToolbox) -> Agent:
    model = AnthropicModel(
        client_args={"api_key": "fake", "http_client": script.client()},
        model_id="claude-opus-5",
        max_tokens=1500,
    )
    return Agent(model=model, tools=agent_tools(toolbox), callback_handler=None)


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


def test_a_scripted_agent_calls_tools_and_the_toolbox_remembers(toolbox):
    answer = "The current ratio {{current_ratio:2025->2026}}, with cash returned to holders [P50]."
    script = ScriptedClaude([
        tool_call("t1", "get_metrics", ids=["current_ratio"]),
        tool_call("t2", "search_filing", query="liquidity cash"),
        text(answer),
    ])
    result = agent_for(script, toolbox)("How liquid is it?")

    assert str(result).strip() == answer
    assert len(script.requests) == 3
    assert {t["name"] for t in script.requests[0]["tools"]} == {
        "list_metrics", "get_metrics", "search_filing",
    }
    # The tool's output went back to the model as a tool result.
    results = [c for m in script.requests[1]["messages"] if isinstance(m["content"], list)
               for c in m["content"] if c.get("type") == "tool_result"]
    assert "{{current_ratio:2026}}" in str(results)
    # And the answer passes the memo's checks, because it used only what it was shown.
    assert ("current_ratio", 2026) in toolbox.shown_metrics
    assert "P50" in toolbox.shown_passages
    assert toolbox.check(answer) == []


def test_a_made_up_metric_id_reaches_the_tool_and_is_refused(toolbox):
    script = ScriptedClaude([
        tool_call("t1", "get_metrics", ids=["liquidity_vibes"]),
        text("The filing has no such metric."),
    ])
    agent_for(script, toolbox)("How liquid is it?")
    returned = str(script.requests[1]["messages"][-1])
    assert "Not metric ids" in returned
    assert toolbox.shown_metrics == {}
