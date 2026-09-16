"""Step 5: the Claude calls, driven by a fake client.

Nothing here reaches the network. The point is the request we build, the reply
we accept, and the replies we refuse to accept.
"""

from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest

from fin_analyst.config import load_settings
from fin_analyst.graph import AnalysisFailed
from fin_analyst.llm import (
    ClaudeAnalyst,
    ModelRefused,
    PlanChoice,
    ResponseTruncated,
    describe_values,
    plan_tool,
    writer_prompt,
)
from fin_analyst.metrics import METRICS_BY_ID, MetricResult

METRICS = [
    MetricResult(metric_id="roe", fiscal_year=2026, value=0.302336, unit="percent"),
    MetricResult(metric_id="roe", fiscal_year=2025, value=0.296472, unit="percent"),
    MetricResult(
        metric_id="debt_to_equity",
        fiscal_year=2026,
        value=None,
        unit="ratio",
        reason="equity is negative for 2026",
    ),
]


def reply(blocks, stop_reason="end_turn", input_tokens=1000, output_tokens=400):
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        stop_details=None,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(**kwargs):
    return SimpleNamespace(type="tool_use", name="choose_metrics", input=kwargs)


class FakeClient:
    """Records the request and returns whatever reply the test set up."""

    def __init__(self, response):
        self.response = response
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        self.requests.append(request)
        return self.response


@pytest.fixture
def settings():
    return load_settings()


def analyst(settings, response, **kwargs):
    client = FakeClient(response)
    return ClaudeAnalyst(settings, client=client, **kwargs), client


# --- the planner ----------------------------------------------------------


def test_the_tool_only_allows_metrics_that_exist():
    schema = plan_tool()["input_schema"]
    assert schema["properties"]["metric_ids"]["items"]["enum"] == sorted(METRICS_BY_ID)
    # Strict tool use requires both of these, or Claude's arguments aren't validated.
    assert plan_tool()["strict"] is True
    assert schema["additionalProperties"] is False
    # The API rejects array length limits in a strict tool schema, so the count
    # is enforced by PlanChoice instead. Removing this guard reintroduces a 400.
    assert "maxItems" not in schema["properties"]["metric_ids"]
    assert "minItems" not in schema["properties"]["metric_ids"]


def test_the_plan_comes_back_as_metric_ids_and_a_cost(settings):
    chosen = ["roe", "net_margin"]
    picked, cost = analyst(settings, reply([tool_block(metric_ids=chosen)]))[0].choose_metrics(
        "MSFT", "What drives ROE?"
    )
    assert picked == chosen
    # 1000 input + 400 output at Opus 5 rates.
    assert cost == pytest.approx(1000 * 5 / 1e6 + 400 * 25 / 1e6)


def test_a_plan_with_no_tool_call_is_an_error(settings):
    a, _ = analyst(settings, reply([text_block("I would use the current ratio.")]))
    with pytest.raises(AnalysisFailed, match="no tool call"):
        a.choose_metrics("MSFT", "How liquid is it?")


def test_a_plan_that_breaks_its_own_schema_is_rejected(settings):
    with pytest.raises(ValueError):
        PlanChoice.model_validate({"metric_ids": [], "note": "extra"})


def test_too_many_metrics_is_a_recorded_failure_not_a_crash(settings):
    a, _ = analyst(settings, reply([tool_block(metric_ids=sorted(METRICS_BY_ID))]))
    with pytest.raises(AnalysisFailed, match="not usable"):
        a.choose_metrics("MSFT", "tell me everything")


def test_a_two_part_question_may_use_more_than_five_metrics(settings):
    """Liquidity plus the DuPont breakdown is legitimately seven."""
    seven = ["current_ratio", "quick_ratio", "cash_flow_ratio", "roe", "net_margin",
             "asset_turnover", "equity_multiplier"]
    picked, _ = analyst(settings, reply([tool_block(metric_ids=seven)]))[0].choose_metrics(
        "MSFT", "How liquid is it, and what drives ROE?"
    )
    assert picked == seven


def test_spending_is_counted_even_when_the_reply_is_rejected(settings):
    a, _ = analyst(settings, reply([text_block("x")], stop_reason="max_tokens"))
    with pytest.raises(ResponseTruncated):
        a.write_draft("MSFT", "q", METRICS, [])
    assert a.spent_usd > 0


# --- the writer -----------------------------------------------------------


def test_the_writer_sees_values_and_the_placeholders_that_exist():
    prompt = writer_prompt("What drives ROE?", METRICS, problems=[])
    assert "{{roe:2026}} = 30.2%" in prompt
    assert "{{debt_to_equity:2026}} = unavailable: equity is negative for 2026" in prompt
    assert "Change placeholders" in prompt


def test_a_retry_tells_the_writer_exactly_what_was_wrong():
    prompt = writer_prompt("q", METRICS, problems=["'1.23' is a number you wrote yourself"])
    assert "previous draft was rejected" in prompt
    assert "'1.23' is a number you wrote yourself" in prompt


def test_a_single_year_gets_no_change_placeholders():
    one_year = [MetricResult(metric_id="roe", fiscal_year=2026, value=0.3, unit="percent")]
    assert "Change placeholders" not in describe_values(one_year)


def test_the_draft_comes_back_as_text(settings):
    a, _ = analyst(settings, reply([text_block("ROE was {{roe:2026}}.")]))
    draft, cost = a.write_draft("MSFT", "q", METRICS, [])
    assert draft == "ROE was {{roe:2026}}."
    assert cost > 0


# --- what the request looks like -----------------------------------------


def test_the_request_uses_this_node_s_settings(settings):
    a, client = analyst(settings, reply([text_block("ok")]))
    a.write_draft("MSFT", "q", METRICS, [])

    request = client.requests[0]
    node = settings.nodes["write"]
    assert request["model"] == node.model
    assert request["max_tokens"] == node.max_tokens
    assert request["output_config"] == {"effort": node.effort}
    assert request["thinking"] == {"type": "adaptive"}  # no budget_tokens on Opus 5
    assert "temperature" not in request  # Opus 5 rejects it


def test_refusal_fallbacks_are_on_by_default_and_can_be_turned_off(settings):
    a, client = analyst(settings, reply([text_block("ok")]))
    a.write_draft("MSFT", "q", METRICS, [])
    assert client.requests[0]["fallbacks"] == "default"
    assert client.requests[0]["betas"] == ["server-side-fallback-2026-07-01"]

    plain, plain_client = analyst(settings, reply([text_block("ok")]), refusal_fallbacks=False)
    plain.write_draft("MSFT", "q", METRICS, [])
    assert "fallbacks" not in plain_client.requests[0]


# --- replies we refuse to accept -----------------------------------------


def test_a_refusal_stops_the_run(settings):
    a, _ = analyst(settings, reply([text_block("")], stop_reason="refusal"))
    with pytest.raises(ModelRefused):
        a.write_draft("MSFT", "q", METRICS, [])


def test_a_truncated_memo_is_not_published(settings):
    a, _ = analyst(settings, reply([text_block("half a mem")], stop_reason="max_tokens"))
    with pytest.raises(ResponseTruncated, match="ceiling"):
        a.write_draft("MSFT", "q", METRICS, [])


def test_an_empty_reply_is_an_error(settings):
    a, _ = analyst(settings, reply([]))
    with pytest.raises(AnalysisFailed, match="no text"):
        a.write_draft("MSFT", "q", METRICS, [])


# --- API failures are recorded, not raised as stack traces ----------------


class FailingClient:
    def __init__(self, error):
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._raise))
        self.messages = SimpleNamespace(create=self._raise)
        self.error = error

    def _raise(self, **request):
        raise self.error


def api_error(cls, status_code, message="nope"):
    """A real SDK error object, built the way the SDK builds one."""
    response = httpx.Response(
        status_code=status_code,
        headers={"retry-after": "30"},
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    return cls(message, response=response, body=None)


@pytest.mark.parametrize(
    "cls, status, expected",
    [
        (anthropic.AuthenticationError, 401, "rejected the key"),
        (anthropic.RateLimitError, 429, "retry after 30"),
        (anthropic.InternalServerError, 500, "returned 500"),
    ],
)
def test_api_failures_become_recorded_run_failures(settings, cls, status, expected):
    a = ClaudeAnalyst(settings, client=FailingClient(api_error(cls, status)))
    with pytest.raises(AnalysisFailed, match=expected):
        a.write_draft("MSFT", "q", METRICS, [])
