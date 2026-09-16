"""Ask one question about several companies.

    python scripts/batch.py "How liquid is it?" MSFT COST JPM --dry-run
    python scripts/batch.py "How liquid is it?" MSFT COST JPM --max-usd 0.25
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from fin_analyst.batch import ESTIMATE_PER_MEMO, batch_dir, run_batch
from fin_analyst.config import load_settings
from fin_analyst.passages import fetch_passages


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--dry-run", action="store_true", help="estimate the cost and stop")
    parser.add_argument("--no-text", action="store_true", help="skip the filing's narrative")
    parser.add_argument("--max-usd", type=float, help="stop the batch at this total spend")
    args = parser.parse_args(argv)

    load_dotenv()
    settings = load_settings()
    cap = args.max_usd if args.max_usd is not None else settings.max_usd_per_run
    estimate = len(args.tickers) * ESTIMATE_PER_MEMO

    print(f"Question: {args.question}")
    print(f"Tickers:  {', '.join(t.upper() for t in args.tickers)}")
    print(f"Estimate: ~${estimate:.2f}, stopping at ${cap:.2f}")

    if args.dry_run:
        print("\nDry run: nothing was called and nothing was spent.")
        return 0

    from fin_analyst.llm import ClaudeAnalyst

    out_dir = batch_dir()
    items = run_batch(
        args.tickers,
        args.question,
        ClaudeAnalyst(settings),
        settings,
        out_dir,
        fetch_text=None if args.no_text else fetch_passages,
        max_usd_total=cap,
    )

    print(f"\n{(out_dir / 'summary.md').read_text()}")
    print(f"Memos are in {out_dir}")
    return 0 if any(i.memo_file for i in items) else 1


if __name__ == "__main__":
    raise SystemExit(main())
