import subprocess
import sys

import pytest

from fin_analyst.__main__ import main
from fin_analyst.config import PROJECT_ROOT, load_settings


def write_config(tmp_path, **overrides):
    """A valid config file, with the given lines swapped in."""
    values = {
        "plan_model": "claude-opus-5",
        "effort": "low",
        "max_tokens": 512,
        "max_usd_per_run": 1.0,
        "max_retries": 2,
    }
    values.update(overrides)
    config = tmp_path / "config.yaml"
    config.write_text(
        "nodes:\n"
        f"  plan: {{model: {values['plan_model']}, effort: {values['effort']}, "
        f"max_tokens: {values['max_tokens']}}}\n"
        "prices_per_million_tokens:\n"
        "  claude-opus-5: {input: 5.00, output: 25.00}\n"
        f"max_usd_per_run: {values['max_usd_per_run']}\n"
        f"max_retries: {values['max_retries']}\n"
    )
    return config


def test_help_runs():
    result = subprocess.run(
        [sys.executable, "-m", "fin_analyst", "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "ticker" in result.stdout


def test_dry_run_reports_settings_and_spends_nothing(capsys):
    """--dry-run must never reach Claude, so it is safe in tests."""
    assert main(["msft", "How liquid is Microsoft?", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "MSFT" in output
    assert "How liquid is Microsoft?" in output
    assert "nothing was spent" in output


def test_a_live_run_without_an_api_key_stops_before_calling_anything(capsys, monkeypatch):
    """No key means no call. .env is neutralised too, so this can't reach the API."""
    monkeypatch.setattr("fin_analyst.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["msft", "How liquid is Microsoft?"]) == 1
    assert "No ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_project_config_loads():
    settings = load_settings()
    assert settings.nodes["plan"].model.startswith("claude-")
    assert settings.nodes["write"].max_tokens > 0
    assert settings.max_usd_per_run > 0


def test_cost_uses_the_price_of_the_model_used():
    settings = load_settings()
    # 1M input at $5 plus 1M output at $25.
    assert settings.cost_usd("claude-opus-5", 1_000_000, 1_000_000) == pytest.approx(30.0)
    assert settings.cost_usd("claude-opus-5", 2_000, 400) == pytest.approx(0.02)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"max_usd_per_run": 0}, "max_usd_per_run"),
        ({"max_retries": -1}, "max_retries"),
        ({"plan_model": "claude-made-up-9"}, "no price"),
        ({"effort": "turbo"}, "effort"),
        ({"max_tokens": 0}, "max_tokens"),
    ],
)
def test_bad_config_is_rejected(tmp_path, overrides, message):
    with pytest.raises(ValueError, match=message):
        load_settings(write_config(tmp_path, **overrides))
