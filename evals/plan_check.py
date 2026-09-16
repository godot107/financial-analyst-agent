"""Does the planner pick metrics that actually answer the question?

A plan can be perfectly valid (every id exists, the tool call parses) and still
fail the user by answering a different question - Huyen calls this goal failure,
and the unknown-metric test cannot catch it.

Only the plan node runs here, so each question costs about a cent.

    python evals/plan_check.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from fin_analyst.config import load_settings
from fin_analyst.llm import ClaudeAnalyst

LIQUIDITY = {"current_ratio", "quick_ratio", "cash_flow_ratio"}
LEVERAGE = {"debt_to_equity", "equity_multiplier"}
PROFITABILITY = {"gross_margin", "net_margin", "roe"}

# (question, metrics it must include, groups it must touch at least one of)
CASES = [
    ("How liquid is Microsoft?", {"current_ratio", "quick_ratio"}, []),
    ("Can it cover its short-term obligations from operations?", {"cash_flow_ratio"}, []),
    (
        "What drives its return on equity?",
        {"roe", "net_margin", "asset_turnover", "equity_multiplier"},
        [],
    ),
    ("Is its ROE coming from profitability or from leverage?", {"net_margin", "equity_multiplier"}, []),
    ("How much leverage does it carry?", {"debt_to_equity"}, []),
    ("Did margins expand or contract?", {"gross_margin", "net_margin"}, []),
    ("How efficiently does it use its assets?", {"asset_turnover"}, []),
    ("Is it more or less profitable than last year?", {"net_margin"}, []),
    ("How risky is its balance sheet?", {"debt_to_equity"}, [LIQUIDITY]),
    ("Give me a general financial health check.", set(), [LIQUIDITY, LEVERAGE, PROFITABILITY]),
]


def main() -> int:
    load_dotenv()
    settings = load_settings()
    analyst = ClaudeAnalyst(settings)

    passed = 0
    for question, required, groups in CASES:
        chosen = set(analyst.choose_metrics("MSFT", question)[0])
        missing = required - chosen
        untouched = [g for g in groups if not (g & chosen)]

        ok = not missing and not untouched
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {question}")
        print(f"      chose: {', '.join(sorted(chosen))}")
        if missing:
            print(f"      missing: {', '.join(sorted(missing))}")
        if untouched:
            print(f"      ignored a whole group: {[sorted(g) for g in untouched]}")

    print(f"\n{passed}/{len(CASES)} plans answered the question asked.")
    print(f"Spent ${analyst.spent_usd:.4f}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
