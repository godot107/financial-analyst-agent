"""Follow-up chat with a Strands agent that picks its own tools (docs/STRANDS_PLAN.md).

Interactive use only: a person reads every answer. Anything automated - the API,
Lambda, batch runs, AuraTrade - stays on the fixed pipeline in graph.py.

This is the only file besides llm.py and the entry points that may import
Strands (tests/test_llm_isolation.py), and it only ever builds an
`AnthropicModel`: the agent talks to the Claude API directly, never Bedrock.
The tools themselves are plain Python in chat_tools.py.
"""

from strands import tool

from fin_analyst.chat_tools import ChatToolbox


def agent_tools(toolbox: ChatToolbox) -> list:
    """The toolbox's methods as Strands tools. Bound methods: `self` stays out of the schema."""
    tools = [toolbox.list_metrics, toolbox.get_metrics, toolbox.search_filing]
    # Offer only what this conversation can do, so the model isn't tempted by a
    # tool that will only say "not available".
    if toolbox.fetch_peer is not None:
        tools.append(toolbox.compare_with)
    if toolbox.fetch_news is not None:
        tools.append(toolbox.recent_news)
    return [tool(method) for method in tools]
