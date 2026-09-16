"""What a filing gave us, and what it didn't. Costs nothing to run.

    python scripts/coverage.py MSFT COST JPM
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from fin_analyst.coverage import concerns, line_item_coverage, metric_coverage
from fin_analyst.edgar import fetch_facts


def report(ticker: str) -> int:
    facts = fetch_facts(ticker)
    years = sorted({f.fiscal_year for f in facts}, reverse=True)
    print(f"\n=== {ticker}: {len(facts)} facts, years {years}, filing {facts[0].accession}")

    print("\n  line items")
    for row in line_item_coverage(facts):
        tag = row.concept or "-"
        print(f"    {row.line_item:24s} {row.status:20s} {tag:52s} {row.years}")

    print("\n  metrics")
    for row in metric_coverage(facts):
        note = f"  unavailable: {sorted(row.unavailable)}" if row.unavailable else ""
        print(f"    {row.metric_id:20s} {row.years}{note}")

    found = concerns(facts)
    print("\n  worth checking" if found else "\n  nothing to flag")
    for note in found:
        print(f"    - {note}")
    return len(found)


def main(tickers: list[str]) -> int:
    load_dotenv()
    flagged = sum(report(ticker.upper()) for ticker in tickers)
    print(f"\n{flagged} thing(s) flagged across {len(tickers)} filing(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["MSFT"]))
