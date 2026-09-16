"""Command line: python -m fin_analyst TICKER "question"."""

import argparse
import os
import sys

from fin_analyst.config import load_settings
from fin_analyst.graph import run_analysis


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fin_analyst",
        description="Answer a question about a company with a memo built from its latest 10-K.",
    )
    parser.add_argument("ticker", help="stock ticker, e.g. MSFT")
    parser.add_argument("question", help='what you want to know, e.g. "How liquid is Microsoft?"')
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the settings and stop, without calling Claude or spending anything",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    ticker = args.ticker.upper()

    print(f"Ticker:   {ticker}")
    print(f"Question: {args.question}")
    for name, node in settings.nodes.items():
        print(f"{name + ':':9s} {node.model}, effort {node.effort}, max {node.max_tokens} tokens")
    print(f"Stops if a run passes ${settings.max_usd_per_run:.2f}")

    if args.dry_run:
        print("\nDry run: nothing was called and nothing was spent.")
        return 0

    if not settings.sec_user_agent:
        print("\nSet SEC_USER_AGENT in .env first (your name and email).", file=sys.stderr)
        return 1
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "\nNo ANTHROPIC_API_KEY found. Put one in .env, or run `ant auth login`.",
            file=sys.stderr,
        )
        return 1

    # Imported here so --dry-run and --help work without the anthropic client.
    from fin_analyst.llm import ClaudeAnalyst

    print("\nWorking...\n")
    state = run_analysis(ticker, args.question, ClaudeAnalyst(settings), settings)

    if state.memo:
        print(state.memo)
    else:
        print(f"No memo: {state.error}", file=sys.stderr)
    print(f"\n[{len(state.drafts)} draft(s), ${state.cost_usd:.4f}]", file=sys.stderr)
    return 0 if state.memo else 1


if __name__ == "__main__":
    sys.exit(main())
