"""A scripted Claude for Strands tests: canned streamed replies over a fake HTTP transport.

No socket is opened (conftest.py would refuse it) and no key is used. Each request
gets the next reply in the script, and every request is kept for the test to read.
anthropic 1.x is built on httpx2, so the transport must be httpx2's.
"""

import json

import httpx2


def text(words: str) -> list[dict]:
    return [{"type": "text", "text": words}]


def tool_call(call_id: str, name: str, **arguments) -> list[dict]:
    return [{"type": "tool_use", "id": call_id, "name": name, "input": arguments}]


def _events(blocks: list[dict], input_tokens: int, output_tokens: int) -> list[dict]:
    stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    events = [{"type": "message_start", "message": {
        "id": "msg_fake", "type": "message", "role": "assistant", "model": "claude-opus-5",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": 0}}}]
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        events += [
            {"type": "content_block_start", "index": i, "content_block": start},
            {"type": "content_block_delta", "index": i, "delta": delta},
            {"type": "content_block_stop", "index": i},
        ]
    return events + [
        {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
         "usage": {"output_tokens": output_tokens}},
        {"type": "message_stop"},
    ]


class ScriptedClaude:
    """Replies in order; `requests` holds what Strands actually sent."""

    def __init__(self, replies: list[list[dict]], input_tokens: int = 1000, output_tokens: int = 200):
        self.replies = replies
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.requests: list[dict] = []

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        if len(self.requests) > len(self.replies):
            raise AssertionError(f"the agent made call {len(self.requests)}; the script has {len(self.replies)}")
        events = _events(self.replies[len(self.requests) - 1], self.input_tokens, self.output_tokens)
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle))
