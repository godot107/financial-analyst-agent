"""Record a company's 10-K facts as a test fixture.

    python scripts/record_fixture.py MSFT

Tests run against these files so they need no network. Re-record only on purpose:
a new fixture means a new filing, and the hand-checked values in the metric tests
will need checking again.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from fin_analyst.edgar import fetch_facts, save_facts

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def main(ticker: str) -> int:
    load_dotenv()
    facts = fetch_facts(ticker)
    path = FIXTURES / f"{ticker.lower()}_facts.json"
    save_facts(facts, path)
    print(f"{len(facts)} facts from {facts[0].accession} → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
