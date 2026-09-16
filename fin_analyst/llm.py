"""Step 5: the two calls to Claude.

This is the only file that imports `anthropic`. It implements the `Analyst`
protocol in `graph.py`: choose the metrics, then write the draft. Neither call
is allowed to produce a number — the planner picks ids from an enum, and the
writer writes placeholders.
"""

import anthropic
from pydantic import BaseModel, ConfigDict, Field

from fin_analyst.config import Settings
from fin_analyst.graph import AnalysisFailed
from fin_analyst.metrics import METRICS_BY_ID, MetricResult, describe_metrics

PLANNER_SYSTEM = """You choose which financial ratios answer a question about one company.
You never compute, estimate or state a number.

Choose 2 to 5 metric ids from this list, and nothing else:
{metrics}

- Choose what answers the question asked, not everything related to it.
- For a question about return on equity, include its three DuPont components.
- Return the choice with the choose_metrics tool."""

WRITER_SYSTEM = """You write a short memo about {ticker} using only its latest 10-K.
A Python program supplies every number. You never type one.

Refer to numbers only with placeholders:
  {{{{metric_id:year}}}}          e.g. {{{{current_ratio:2026}}}}
  {{{{metric_id:year->year}}}}    e.g. {{{{net_margin:2025->2026}}}}
                              renders as "rose 1.2 pts to 36.1%", direction included
Only the placeholders listed below exist. Anything else is an error.

Rules:
- No digits outside a placeholder: no percentages, no multiples, no "above 1 is healthy".
  You may name a fiscal year in prose when it appears in the data.
- The values below are for your judgment only. Never repeat one as text.
- Compare the company with its own prior year, and state plainly that there is no
  peer comparison.
- Explain only what the metrics show, such as which DuPont component moved, or
  whether liquidity sits in cash or receivables. Give no business reasons: you do
  not have the filing's text, so any reason would be invented.
- If a metric is unavailable, say so and give the reason. Never work around it.
- If the question needs something this data cannot give - a peer comparison, a
  valuation multiple, or a business explanation - say so in one line.
- About 250 words of markdown, opening with a one-line answer to the question."""


class PlanChoice(BaseModel):
    """What the planner is allowed to return."""

    model_config = ConfigDict(extra="forbid")

    metric_ids: list[str] = Field(min_length=1, max_length=5)


class ModelRefused(AnalysisFailed):
    """Claude declined the request. Record it; don't retry blindly."""


class ResponseTruncated(AnalysisFailed):
    """The reply hit max_tokens. A half-written memo is not a memo."""


def plan_tool() -> dict:
    """A strict tool whose ids are an enum of the registry, so a typo can't get through."""
    return {
        "name": "choose_metrics",
        "description": "Record the metrics that answer the user's question.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric_ids": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(METRICS_BY_ID)},
                    "minItems": 1,
                    "maxItems": 5,
                }
            },
            "required": ["metric_ids"],
            "additionalProperties": False,
        },
        "strict": True,
    }


def describe_values(metrics: list[MetricResult]) -> str:
    """The values and the placeholders that exist, as the writer sees them."""
    lines = []
    for m in sorted(metrics, key=lambda m: (m.metric_id, -m.fiscal_year)):
        if m.value is None:
            lines.append(f"- {{{{{m.metric_id}:{m.fiscal_year}}}}} = unavailable: {m.reason}")
        elif m.unit == "percent":
            lines.append(f"- {{{{{m.metric_id}:{m.fiscal_year}}}}} = {m.value * 100:.1f}%")
        else:
            lines.append(f"- {{{{{m.metric_id}:{m.fiscal_year}}}}} = {m.value:.2f}x")

    years = sorted({m.fiscal_year for m in metrics}, reverse=True)
    if len(years) > 1:
        lines.append(
            f"Change placeholders also exist between any two of these years: {years}, "
            "e.g. {{net_margin:" + f"{years[1]}->{years[0]}" + "}}"
        )
    return "\n".join(lines)


def writer_prompt(question: str, metrics: list[MetricResult], problems: list[str]) -> str:
    parts = [f"Question: {question}", "", "Available placeholders and their values:", describe_values(metrics)]
    if problems:
        parts += [
            "",
            "Your previous draft was rejected. Fix exactly these problems and rewrite it:",
            *(f"- {p}" for p in problems),
        ]
    return "\n".join(parts)


class ClaudeAnalyst:
    """The real Claude, behind the same two methods the tests fake."""

    def __init__(self, settings: Settings, client=None, refusal_fallbacks: bool = True):
        self.settings = settings
        self.client = client or anthropic.Anthropic()
        # A benign financial memo should never be refused, but a refusal would
        # otherwise end the run; the fallback model finishes it instead.
        self.refusal_fallbacks = refusal_fallbacks

    def _call(self, node: str, system: str, user: str, tools: list[dict] | None = None):
        settings = self.settings.nodes[node]
        request = dict(
            model=settings.model,
            max_tokens=settings.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            thinking={"type": "adaptive"},
            output_config={"effort": settings.effort},
        )
        if tools:
            request["tools"] = tools
            request["tool_choice"] = {"type": "tool", "name": tools[0]["name"]}

        try:
            if self.refusal_fallbacks:
                response = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request
                )
            else:
                response = self.client.messages.create(**request)
        except anthropic.AuthenticationError:
            raise AnalysisFailed(
                "the API rejected the key: check ANTHROPIC_API_KEY in .env against the console"
            ) from None
        except anthropic.RateLimitError as limited:
            retry_after = limited.response.headers.get("retry-after", "a moment")
            raise AnalysisFailed(f"rate limited on the {node} step; retry after {retry_after}") from None
        except anthropic.APIStatusError as failed:
            raise AnalysisFailed(
                f"the API returned {failed.status_code} on the {node} step: {failed.message}"
            ) from None
        except anthropic.APIConnectionError:
            raise AnalysisFailed(f"could not reach the API on the {node} step; check the network") from None

        cost = self.settings.cost_usd(
            settings.model, response.usage.input_tokens, response.usage.output_tokens
        )

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise ModelRefused(f"Claude declined the {node} step: {getattr(details, 'explanation', '')}")
        if response.stop_reason == "max_tokens":
            raise ResponseTruncated(
                f"the {node} step hit its {settings.max_tokens}-token ceiling; "
                "raise max_tokens in config.yaml or ask for a shorter memo"
            )
        return response, cost

    def choose_metrics(self, ticker: str, question: str) -> tuple[list[str], float]:
        response, cost = self._call(
            "plan",
            PLANNER_SYSTEM.format(metrics=describe_metrics()),
            f"Company: {ticker}\nQuestion: {question}",
            tools=[plan_tool()],
        )
        for block in response.content:
            if block.type == "tool_use":
                return PlanChoice.model_validate(block.input).metric_ids, cost
        raise AnalysisFailed("the planner returned no tool call, so no metrics were chosen")

    def write_draft(
        self,
        ticker: str,
        question: str,
        metrics: list[MetricResult],
        problems: list[str],
    ) -> tuple[str, float]:
        response, cost = self._call(
            "write",
            WRITER_SYSTEM.format(ticker=ticker),
            writer_prompt(question, metrics, problems),
        )
        draft = "\n".join(b.text for b in response.content if b.type == "text").strip()
        if not draft:
            raise AnalysisFailed("Claude returned no text for the memo")
        return draft, cost
