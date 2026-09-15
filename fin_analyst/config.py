"""Settings for a run: tunables from config.yaml, secrets from .env."""

from dataclasses import dataclass
from pathlib import Path
import os

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@dataclass(frozen=True)
class Settings:
    model: str
    input_price: float  # USD per million input tokens
    output_price: float  # USD per million output tokens (thinking tokens bill as output)
    max_usd_per_run: float
    max_retries: int
    sec_user_agent: str | None  # SEC rejects downloads without one


def load_settings(config_path: Path = CONFIG_PATH) -> Settings:
    # Copies .env into the environment, where the anthropic SDK finds ANTHROPIC_API_KEY.
    # Variables already set in the shell win.
    load_dotenv(PROJECT_ROOT / ".env")

    raw = yaml.safe_load(config_path.read_text())
    prices = raw["price_per_million_tokens"]
    settings = Settings(
        model=raw["model"],
        input_price=float(prices["input"]),
        output_price=float(prices["output"]),
        max_usd_per_run=float(raw["max_usd_per_run"]),
        max_retries=int(raw["max_retries"]),
        sec_user_agent=os.environ.get("SEC_USER_AGENT") or None,
    )

    if settings.max_usd_per_run <= 0:
        raise ValueError("max_usd_per_run must be greater than 0")
    if settings.max_retries < 0:
        raise ValueError("max_retries cannot be negative")
    return settings
