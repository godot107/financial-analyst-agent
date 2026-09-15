import subprocess
import sys

import pytest

from fin_analyst.__main__ import main
from fin_analyst.config import PROJECT_ROOT, load_settings


def test_help_runs():
    result = subprocess.run(
        [sys.executable, "-m", "fin_analyst", "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "ticker" in result.stdout


def test_cli_echoes_request(capsys):
    assert main(["msft", "How liquid is Microsoft?"]) == 0
    output = capsys.readouterr().out
    assert "MSFT" in output
    assert "How liquid is Microsoft?" in output


def test_project_config_loads():
    settings = load_settings()
    assert settings.model == "claude-opus-5"
    assert settings.max_usd_per_run > 0
    assert settings.max_retries >= 0


def test_zero_budget_is_rejected(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "model: claude-opus-5\n"
        "price_per_million_tokens: {input: 5, output: 25}\n"
        "max_usd_per_run: 0\n"
        "max_retries: 2\n"
    )
    with pytest.raises(ValueError, match="max_usd_per_run"):
        load_settings(config)
