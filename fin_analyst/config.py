"""Settings for a run: tunables from config.yaml, secrets from .env."""

from dataclasses import dataclass, field
from pathlib import Path
import os

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}
PROVIDERS = ("anthropic", "bedrock")
# Every follow-up resends the answers before it, so a long chat costs more per
# turn than the one before. Five is the most this project will run.
CHAT_TURN_CEILING = 5


@dataclass(frozen=True)
class NodeSettings:
    """How one Claude-using node calls the model."""

    model: str
    effort: str
    max_tokens: int


@dataclass(frozen=True)
class BedrockModel:
    """One Claude model on Amazon Bedrock, and what it supports there."""

    id: str  # the inference profile called when profile_env is unset
    profile_env: str | None = None  # names the variable with a tagged profile ARN
    effort: bool = False  # the effort setting (a beta on Opus 4.5)
    strict_tools: bool = True

    def invoke_id(self) -> str:
        """The tagged application inference profile if one is set, else the global profile."""
        return (os.environ.get(self.profile_env) if self.profile_env else None) or self.id


@dataclass(frozen=True)
class Settings:
    nodes: dict[str, NodeSettings]
    # model -> (USD per million input tokens, USD per million output tokens)
    prices: dict[str, tuple[float, float]]
    max_usd_per_run: float
    max_retries: int
    chat_max_turns: int
    sec_user_agent: str | None  # SEC rejects downloads without one
    api_per_key_daily_usd: float = 1.00  # the HTTP service's caps, per UTC day
    api_global_daily_usd: float = 3.00
    # Ask Claude for a summary of its thinking, for the run's trace.
    thinking_summaries: bool = True
    # The Strands chat agent's tool calls per question (strands_chat.py).
    chat_max_tool_calls: int = 8
    # "anthropic" (the Claude API) or "bedrock" (Amazon Bedrock).
    provider: str = "anthropic"
    bedrock_region: str = "us-east-1"
    bedrock_models: dict[str, BedrockModel] = field(default_factory=dict)
    # node -> manual thinking budget on Bedrock (nodes that force a tool can't think)
    bedrock_thinking: dict[str, int] = field(default_factory=dict)

    def bedrock_model(self, model: str) -> BedrockModel:
        if model not in self.bedrock_models:
            raise ValueError(f"{model} has no entry under bedrock.models in config.yaml")
        return self.bedrock_models[model]

    def cost_usd(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """What one call cost. Thinking tokens are billed as output."""
        input_price, output_price = self.prices[model]
        return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def load_settings(config_path: Path = CONFIG_PATH) -> Settings:
    # Copies .env into the environment, where the anthropic SDK finds
    # ANTHROPIC_API_KEY (Bedrock needs none: it uses AWS credentials).
    # Variables already set in the shell win.
    load_dotenv(PROJECT_ROOT / ".env")

    raw = yaml.safe_load(config_path.read_text())
    provider = os.environ.get("FIN_ANALYST_PROVIDER") or raw.get("provider", "anthropic")
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {PROVIDERS}, not {provider!r}")
    bedrock = raw.get("bedrock") or {}
    # On Bedrock each node keeps its effort and ceiling but uses the model listed there.
    swap = bedrock.get("nodes", {}) if provider == "bedrock" else {}
    settings = Settings(
        nodes={
            name: NodeSettings(
                model=swap.get(name, node["model"]),
                effort=node["effort"],
                max_tokens=int(node["max_tokens"]),
            )
            for name, node in raw["nodes"].items()
        },
        provider=provider,
        bedrock_region=bedrock.get("region", "us-east-1"),
        bedrock_thinking={node: int(n) for node, n in (bedrock.get("thinking_budget") or {}).items()},
        bedrock_models={
            name: BedrockModel(
                id=model["id"],
                profile_env=model.get("profile_env"),
                effort=bool(model.get("effort", False)),
                strict_tools=bool(model.get("strict_tools", True)),
            )
            for name, model in (bedrock.get("models") or {}).items()
        },
        prices={
            model: (float(price["input"]), float(price["output"]))
            for model, price in raw["prices_per_million_tokens"].items()
        },
        max_usd_per_run=float(raw["max_usd_per_run"]),
        max_retries=int(raw["max_retries"]),
        chat_max_turns=int(raw.get("chat_max_turns", CHAT_TURN_CEILING)),
        sec_user_agent=os.environ.get("SEC_USER_AGENT") or None,
        api_per_key_daily_usd=float(raw.get("api", {}).get("per_key_daily_usd", 1.00)),
        api_global_daily_usd=float(raw.get("api", {}).get("global_daily_usd", 3.00)),
        thinking_summaries=bool(raw.get("thinking_summaries", True)),
        chat_max_tool_calls=int(raw.get("chat_max_tool_calls", 8)),
    )

    if settings.max_usd_per_run <= 0:
        raise ValueError("max_usd_per_run must be greater than 0")
    if settings.max_retries < 0:
        raise ValueError("max_retries cannot be negative")
    if settings.api_per_key_daily_usd <= 0 or settings.api_global_daily_usd <= 0:
        raise ValueError("api daily caps must be greater than 0")
    if not 1 <= settings.chat_max_turns <= CHAT_TURN_CEILING:
        raise ValueError(f"chat_max_turns must be between 1 and {CHAT_TURN_CEILING}")
    if settings.chat_max_tool_calls < 1:
        raise ValueError("chat_max_tool_calls must be at least 1")
    for name, node in settings.nodes.items():
        if node.model not in settings.prices:
            raise ValueError(
                f"node '{name}' uses {node.model}, which has no price in "
                "prices_per_million_tokens; the budget guard needs one"
            )
        if node.effort not in EFFORT_LEVELS:
            raise ValueError(f"node '{name}' has effort '{node.effort}'; use one of {EFFORT_LEVELS}")
        if node.max_tokens <= 0:
            raise ValueError(f"node '{name}' needs a positive max_tokens")
        if provider == "bedrock" and node.model not in settings.bedrock_models:
            raise ValueError(f"node '{name}' uses {node.model}, which has no bedrock.models entry")
    for node, budget in settings.bedrock_thinking.items():
        if node not in settings.nodes or budget < 1024:
            raise ValueError(f"thinking_budget for '{node}': the node must exist and the budget be at least 1024")
    return settings
