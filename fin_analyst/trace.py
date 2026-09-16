"""What happened inside a run, step by step, as it happened.

Every node records what it did and how long it took; every Claude call records
its tokens, cost, stop reason and a summary of its thinking. The events are kept
with the run record and the API's job result, and can also be streamed as they
happen: pretty-printed for `--verbose`, or as JSON lines for CloudWatch.

No model import: the trace only receives what llm.py and graph.py hand it.
"""

import json
import sys
import time
from datetime import datetime, timezone
from typing import Callable, TextIO

from pydantic import BaseModel, Field


class TraceEvent(BaseModel):
    at: str  # UTC timestamp
    seconds: float  # since the trace started
    step: str  # plan, fetch, ..., or "claude" for a model call, "route" for a decision
    event: str
    data: dict = Field(default_factory=dict)


Sink = Callable[[TraceEvent], None]


class Tracer:
    """Collects events and passes each one to the sinks as it arrives."""

    def __init__(self, sinks: list[Sink] = (), context: dict | None = None):
        self.sinks = list(sinks)
        self.context = context or {}  # e.g. the job id, added to every JSON line
        self.events: list[TraceEvent] = []
        self._started = time.monotonic()

    def emit(self, step: str, event: str, **data) -> TraceEvent:
        record = TraceEvent(
            at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            seconds=round(time.monotonic() - self._started, 3),
            step=step,
            event=event,
            data=data,
        )
        self.events.append(record)
        for sink in self.sinks:
            try:
                sink(record)
            except Exception:  # a broken log line must never break a run
                pass
        return record


def json_lines(context: dict | None = None, stream: TextIO | None = None) -> Sink:
    """One JSON object per line: what CloudWatch Logs Insights can query."""

    def sink(event: TraceEvent) -> None:
        line = {"trace": event.model_dump(), **(context or {})}
        print(json.dumps(line, default=str), file=stream or sys.stdout, flush=True)

    return sink


def pretty(stream: TextIO | None = None, width: int = 100) -> Sink:
    """Readable lines for a terminal: the step, then the parts worth reading."""

    def out(text: str = "") -> None:
        print(text, file=stream or sys.stderr, flush=True)

    def wrap(label: str, text: str) -> None:
        indent = " " * 11
        lines = []
        for paragraph in str(text).strip().splitlines():
            words, line = paragraph.split(), ""
            for word in words:
                if line and len(line) + len(word) + 1 > width - len(indent):
                    lines.append(line)
                    line = word
                else:
                    line = f"{line} {word}".strip()
            lines.append(line)
        out(f"{indent}{label}:")
        for line in lines:
            out(f"{indent}  {line}")

    def sink(event: TraceEvent) -> None:
        data = dict(event.data)
        head = f"[{event.seconds:6.1f}s] {event.step:<8} {event.event}"
        long_fields = {k: data.pop(k) for k in ("thinking", "draft", "text") if data.get(k)}
        for key, value in data.items():  # short lists read better on one line
            if isinstance(value, list) and len(", ".join(map(str, value))) <= 60:
                data[key] = ", ".join(map(str, value))
        listed = {k: data.pop(k) for k in list(data) if isinstance(data[k], (list, dict))}
        inline = "  ".join(f"{k}={v}" for k, v in data.items() if v not in (None, ""))
        out(f"{head}  {inline}".rstrip())
        for key, value in listed.items():
            if not value:
                continue
            if isinstance(value, dict):
                value = [f"{k}: {v}" for k, v in value.items()]
            out(f"{' ' * 11}{key}:")
            for item in value:
                out(f"{' ' * 13}- {item}")
        for key, value in long_fields.items():
            wrap(key, value)

    return sink
