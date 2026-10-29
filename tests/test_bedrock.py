"""Claude on Amazon Bedrock (docs/AGENTCORE_PLAN.md, Phase 0): the provider switch.

Same SDK, same Messages API, a different client and a few different request fields.
No test reaches AWS: the client is a fake, as in test_llm.py.
"""

from types import SimpleNamespace

import anthropic
import httpx
import pytest

from fin_analyst.config import load_settings
from fin_analyst.graph import AnalysisFailed
from fin_analyst.llm import EFFORT_BETA, ClaudeAnalyst, make_client

OPUS_45 = "global.anthropic.claude-opus-4-5-20251101-v1:0"
HAIKU_45 = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


class FakeClient:
    """Records each request and which API (plain or beta) it went to."""

    def __init__(self, response):
        self.response = response
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._beta))
        self.messages = SimpleNamespace(create=self._plain)

    def _beta(self, **request):
        self.requests.append(("beta", request))
        return self.response

    def _plain(self, **request):
        self.requests.append(("plain", request))
        return self.response


class FailingClient:
    def __init__(self, error):
        self.error = error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._raise))
        self.messages = SimpleNamespace(create=self._raise)

    def _raise(self, **request):
        raise self.error


def reply(*blocks, stop_reason="end_turn"):
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=400),
        model=OPUS_45,
    )


def text(value):
    return SimpleNamespace(type="text", text=value)


def tool_use(**kwargs):
    return SimpleNamespace(type="tool_use", name="choose_metrics", input=kwargs)


@pytest.fixture
def bedrock(monkeypatch):
    monkeypatch.setenv("FIN_ANALYST_PROVIDER", "bedrock")
    for name in ("FIN_ANALYST_PROFILE_OPUS_4_5", "FIN_ANALYST_PROFILE_HAIKU_4_5"):
        monkeypatch.delenv(name, raising=False)
    return load_settings()


# --- config ---------------------------------------------------------------------


def test_the_default_provider_is_the_claude_api(monkeypatch):
    monkeypatch.delenv("FIN_ANALYST_PROVIDER", raising=False)
    settings = load_settings()
    assert settings.provider == "anthropic"
    assert settings.nodes["write"].model == "claude-opus-5"


def test_bedrock_swaps_each_node_to_a_model_this_account_can_use(bedrock):
    assert bedrock.provider == "bedrock"
    assert {node.model for node in bedrock.nodes.values()} == {"claude-opus-4-5"}
    # effort and ceilings stay as config.yaml sets them for the Claude API
    assert bedrock.nodes["plan"].effort == "low" and bedrock.nodes["write"].max_tokens == 1500
    assert bedrock.cost_usd("claude-opus-4-5", 1_000_000, 0) == 5.00


def test_a_tagged_inference_profile_wins_over_the_global_one(bedrock, monkeypatch):
    model = bedrock.bedrock_model("claude-opus-4-5")
    assert model.invoke_id() == OPUS_45
    arn = "arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/abc"
    monkeypatch.setenv("FIN_ANALYST_PROFILE_OPUS_4_5", arn)
    assert model.invoke_id() == arn


def test_an_unknown_provider_is_refused(monkeypatch):
    monkeypatch.setenv("FIN_ANALYST_PROVIDER", "openai")
    with pytest.raises(ValueError, match="provider"):
        load_settings()


def test_the_client_matches_the_provider(bedrock, monkeypatch):
    assert isinstance(make_client(bedrock), anthropic.AnthropicBedrock)
    monkeypatch.setenv("FIN_ANALYST_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    assert isinstance(make_client(load_settings()), anthropic.Anthropic)


# --- the request ----------------------------------------------------------------


def test_opus_45_gets_effort_through_its_beta_and_manual_thinking_when_writing(bedrock):
    bedrock.bedrock_thinking["write"] = 2048  # the retry arm; off in config.yaml
    client = FakeClient(reply(text("ok")))
    ClaudeAnalyst(bedrock, client=client).write_draft("MSFT", "q", [], [])
    api, request = client.requests[0]
    assert api == "beta" and request["betas"] == [EFFORT_BETA]
    assert request["model"] == OPUS_45
    assert request["output_config"] == {"effort": "medium"}
    # manual thinking (no adaptive on Opus 4.5); the memo keeps its own 1500-token ceiling
    assert request["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert request["max_tokens"] == 1500 + 2048
    assert "fallbacks" not in request  # a Claude API feature


def test_a_step_that_forces_a_tool_never_thinks(bedrock):
    bedrock.bedrock_thinking["plan"] = 2048  # even if configured, a forced tool forbids it
    client = FakeClient(reply(tool_use(metric_ids=["current_ratio"])))
    ClaudeAnalyst(bedrock, client=client).choose_metrics("MSFT", "How liquid?")
    request = client.requests[0][1]
    assert "thinking" not in request and request["max_tokens"] == 512


def test_a_thinking_budget_below_the_minimum_is_refused(tmp_path, monkeypatch):
    from fin_analyst.config import CONFIG_PATH

    monkeypatch.setenv("FIN_ANALYST_PROVIDER", "bedrock")
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG_PATH.read_text().replace("thinking_budget: {}", "thinking_budget: {write: 500}"))
    with pytest.raises(ValueError, match="at least 1024"):
        load_settings(config)


def test_the_planner_still_forces_its_strict_tool(bedrock):
    client = FakeClient(reply(tool_use(metric_ids=["current_ratio"])))
    ids, _ = ClaudeAnalyst(bedrock, client=client).choose_metrics("MSFT", "How liquid?")
    assert ids == ["current_ratio"]
    _, request = client.requests[0]
    assert request["tool_choice"] == {"type": "tool", "name": "choose_metrics"}
    assert request["tools"][0]["strict"] is True


def test_haiku_45_has_no_effort_so_the_plain_api_is_used(bedrock):
    settings = load_settings()
    settings.nodes["write"] = type(settings.nodes["write"])("claude-haiku-4-5", "medium", 1500)
    client = FakeClient(reply(text("ok")))
    ClaudeAnalyst(settings, client=client).write_draft("MSFT", "q", [], [])
    api, request = client.requests[0]
    assert api == "plain" and request["model"] == HAIKU_45
    assert "output_config" not in request and "betas" not in request


def test_a_model_without_strict_tools_gets_them_without_the_flag(bedrock, monkeypatch):
    import dataclasses

    model = bedrock.bedrock_models["claude-opus-4-5"]
    bedrock.bedrock_models["claude-opus-4-5"] = dataclasses.replace(model, strict_tools=False)
    client = FakeClient(reply(tool_use(metric_ids=["current_ratio"])))
    ClaudeAnalyst(bedrock, client=client).choose_metrics("MSFT", "How liquid?")
    assert "strict" not in client.requests[0][1]["tools"][0]


def test_cost_is_priced_as_the_bedrock_model(bedrock):
    client = FakeClient(reply(text("ok")))
    analyst = ClaudeAnalyst(bedrock, client=client)
    _, cost = analyst.write_draft("MSFT", "q", [], [])
    assert cost == pytest.approx((1000 * 5.00 + 400 * 25.00) / 1_000_000)
    assert analyst.tracer.events[-1].data["model"] == OPUS_45


# --- failures name the AWS cause, not an API key --------------------------------


def status_error(cls, status_code, message):
    request = httpx.Request("POST", "https://bedrock-runtime.us-east-1.amazonaws.com")
    return cls(message, response=httpx.Response(status_code, request=request), body=None)


def test_no_model_access_is_named(bedrock):
    error = status_error(anthropic.PermissionDeniedError, 403, "You don't have access to the model")
    with pytest.raises(AnalysisFailed, match="Bedrock refused .*no access to that model"):
        ClaudeAnalyst(bedrock, client=FailingClient(error)).write_draft("MSFT", "q", [], [])


def test_missing_aws_credentials_are_named(bedrock):
    from botocore.exceptions import NoCredentialsError

    with pytest.raises(AnalysisFailed, match="no usable AWS credentials"):
        ClaudeAnalyst(bedrock, client=FailingClient(NoCredentialsError())).write_draft(
            "MSFT", "q", [], []
        )


def test_any_other_error_is_not_swallowed(bedrock):
    with pytest.raises(KeyError):
        ClaudeAnalyst(bedrock, client=FailingClient(KeyError("x"))).write_draft("MSFT", "q", [], [])


# --- the Strands chat agent -----------------------------------------------------


def test_the_chat_agent_uses_bedrock_s_model_class(bedrock):
    from strands.models.bedrock import BedrockModel

    from fin_analyst.strands_chat import build_model

    import boto3

    fake = boto3.Session(aws_access_key_id="x", aws_secret_access_key="y", region_name="us-east-1")
    model = build_model(bedrock, boto_session=fake)
    assert isinstance(model, BedrockModel)
    config = model.get_config()
    assert config["model_id"] == OPUS_45 and config["max_tokens"] == 1500
    assert config["additional_request_fields"]["anthropic_beta"] == [EFFORT_BETA]
