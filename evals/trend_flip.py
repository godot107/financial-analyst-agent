"""Does the prose follow the data, or the model's memory of Microsoft?

Claude has seen Microsoft's real financials in training. The placeholders already
guarantee the *numbers* in the memo, so the only way memory can leak in is through
the *words*: "margins expanded", "improved", "strengthened".

So: reverse a real trend in the recorded filing, run the workflow on the altered
data, and read what Claude wrote. Microsoft's net margin really rose in FY2026;
here net income is cut so it falls sharply instead.

    python evals/trend_flip.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from fin_analyst.config import load_settings
from fin_analyst.edgar import load_facts
from fin_analyst.graph import run_analysis
from fin_analyst.llm import ClaudeAnalyst
from fin_analyst.metrics import compute_all

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "msft_facts.json"
QUESTION = "Is Microsoft more profitable than last year, and what drove the change?"

# Words that assert a direction or a judgment. In a draft they can only have been
# typed by Claude: every real figure and its direction arrives via a placeholder.
IMPROVED = r"\b(?:improv\w+|expand\w+|strengthen\w+|better|stronger|grew|growth|higher|gains?|gained)\b"
WORSENED = r"\b(?:declin\w+|contract\w+|weaken\w+|worse|lower|fell|fall\w*|slide|slid|erosion|eroded|drag|deteriorat\w+)\b"


def perturb(facts, factor=0.45):
    """Cut the latest year's net income, so net margin and ROE fall instead of rise."""
    return [
        f.model_copy(update={"value": f.value * factor})
        if (f.line_item == "net_income" and f.fiscal_year == 2026)
        else f
        for f in facts
    ]


def main() -> int:
    load_dotenv()
    settings = load_settings()
    facts = perturb(load_facts(FIXTURE))

    before = {
        (m.metric_id, m.fiscal_year): m.value
        for m in compute_all(load_facts(FIXTURE), ["net_margin", "roe"])
    }
    after = {(m.metric_id, m.fiscal_year): m.value for m in compute_all(facts, ["net_margin", "roe"])}
    print("What the filing really says, and what this run was given:")
    for key in sorted(before):
        print(f"  {key[0]} {key[1]}: real {before[key]:.1%} -> altered {after[key]:.1%}")

    analyst = ClaudeAnalyst(settings)
    state = run_analysis("MSFT", QUESTION, analyst, settings, fetch=lambda ticker: facts)

    if not state.memo:
        print(f"\nNo memo: {state.error}")
        return 1

    draft = state.drafts[-1]
    print("\n--- draft (what Claude actually wrote) ---\n")
    print(draft)
    print("\n--- rendered memo ---\n")
    print(state.memo)

    print("\n--- sentences claiming a direction ---")
    flagged = 0
    for sentence in re.split(r"(?<=[.!?])\s+", draft):
        improved = re.search(IMPROVED, sentence, re.I)
        worsened = re.search(WORSENED, sentence, re.I)
        if improved or worsened:
            flagged += 1
            verdict = "SAYS IMPROVED" if improved and not worsened else "says worse/mixed"
            print(f"  [{verdict}] {sentence.strip()}")
    if not flagged:
        print("  (none: the direction came only from placeholders)")

    print(f"\nSpent ${analyst.spent_usd:.4f}. Read the sentences above: any claim that profitability")
    print("improved would be memory, because in this data it fell.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
