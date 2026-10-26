"""Step 0 spike: drive a Strands agent through a fake HTTP transport (no network, no spend).

Checks: anthropic 1.6.0 compatibility, params pass-through (thinking, output_config,
fallback beta), the @tool schema Strands sends, hooks (cancel_tool, resume), and metrics.

Run: pip install -r requirements-chat.txt, then python scripts/strands_spike.py. Free: a fake
transport answers, so no key is used and nothing is spent. Findings: docs/STRANDS_PLAN.md.
"""

import json

import httpx2 as httpx
from strands import Agent, tool
from strands.hooks import AfterInvocationEvent, BeforeToolCallEvent
from strands.models.anthropic import AnthropicModel

SENT: list[dict] = []


def sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def message(blocks: list[dict], stop: str) -> list[dict]:
    events = [{"type": "message_start", "message": {
        "id": "msg_x", "type": "message", "role": "assistant", "model": "claude-opus-5",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 0}}}]
    for i, b in enumerate(blocks):
        if b["type"] == "text":
            events += [
                {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b["text"]}},
            ]
        else:
            events += [
                {"type": "content_block_start", "index": i, "content_block": {
                    "type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}},
                {"type": "content_block_delta", "index": i, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps(b["input"])}},
            ]
        events.append({"type": "content_block_stop", "index": i})
    events += [
        {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
         "usage": {"output_tokens": 50}},
        {"type": "message_stop"},
    ]
    return events


# Scripted model: call get_metrics twice (second is cancelled by the cap), leak a digit,
# then answer cleanly after the resume.
SCRIPT = [
    message([{"type": "tool_use", "id": "t1", "name": "get_metrics",
              "input": {"ids": ["current_ratio"], "years": [2026]}}], "tool_use"),
    message([{"type": "tool_use", "id": "t2", "name": "get_metrics",
              "input": {"ids": ["quick_ratio"], "years": [2026]}}], "tool_use"),
    message([{"type": "text", "text": "The current ratio is 1.23x."}], "end_turn"),
    message([{"type": "text", "text": "The current ratio was {{current_ratio:2026}}."}], "end_turn"),
]


def handler(request: httpx.Request) -> httpx.Response:
    SENT.append({"headers": dict(request.headers), "body": json.loads(request.content)})
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=sse(SCRIPT[len(SENT) - 1]))


@tool
def get_metrics(ids: list[str], years: list[int]) -> dict:
    """Compute ratios for the session's company.

    Args:
        ids: Metric ids from list_metrics.
        years: Fiscal years wanted.
    """
    return {"values": {f"{i}:{y}": 1.23 for i in ids for y in years},
            "placeholders": [f"{{{{{i}:{y}}}}}" for i in ids for y in years]}


calls = {"n": 0}


def cap_tools(event: BeforeToolCallEvent) -> None:
    calls["n"] += 1
    if calls["n"] > 1:
        event.cancel_tool = "Tool budget for this turn is spent. Answer with what you have."


retries = {"n": 0}


def check(event: AfterInvocationEvent) -> None:
    text = str(event.result) if event.result else ""
    if any(c.isdigit() for c in text.replace("2026", "")) and "{{" not in text and retries["n"] < 2:
        retries["n"] += 1
        event.resume = "Your answer contains a digit outside a placeholder. Rewrite it."


model = AnthropicModel(
    client_args={"api_key": "sk-fake", "http_client": httpx.AsyncClient(transport=httpx.MockTransport(handler))},
    model_id="claude-opus-5",
    max_tokens=1500,
    params={
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": "medium"},
        "extra_headers": {"anthropic-beta": "server-side-fallback-2026-07-01"},
        "extra_body": {"fallbacks": "default"},
    },
)
agent = Agent(model=model, tools=[get_metrics], system_prompt="SYSTEM", callback_handler=None)
agent.add_hook(cap_tools)
agent.add_hook(check)
result = agent("How liquid is it?")

print("FINAL:", str(result).strip())
print("requests sent:", len(SENT), "| retries:", retries["n"])
first = SENT[0]
print("beta header:", first["headers"].get("anthropic-beta"))
print("body keys:", sorted(first["body"]))
print("thinking:", first["body"].get("thinking"), "| output_config:", first["body"].get("output_config"),
      "| fallbacks:", first["body"].get("fallbacks"))
print("tool schema:", json.dumps(first["body"]["tools"][0]))
print("cancel message seen by model:",
      [c for m in SENT[2]["body"]["messages"] for c in (m["content"] if isinstance(m["content"], list) else [])
       if c.get("type") == "tool_result"][-1])
print("resume message:", SENT[3]["body"]["messages"][-1])
u = result.metrics.accumulated_usage
print("usage:", u["inputTokens"], u["outputTokens"], "| cycles:", len(result.metrics.cycle_durations))
print("tool_metrics:", {k: (v.call_count, v.success_count, v.error_count) for k, v in result.metrics.tool_metrics.items()})
