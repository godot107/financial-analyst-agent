"""Command line: python -m fin_analyst TICKER "question"."""

import argparse
import sys

from fin_analyst.config import load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fin_analyst",
        description="Answer a question about a company with a memo built from its latest 10-K.",
    )
    parser.add_argument("ticker", help="stock ticker, e.g. MSFT")
    parser.add_argument("question", help='what you want to know, e.g. "How liquid is Microsoft?"')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()

    print(f"Ticker:   {args.ticker.upper()}")
    print(f"Question: {args.question}")
    print(f"Model:    {settings.model} (budget ${settings.max_usd_per_run:.2f} per run)")
    print("The workflow isn't built yet. See PLAN.md, Step 1 onward.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
