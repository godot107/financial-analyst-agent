"""Command line: python -m fin_analyst TICKER "question"."""

import argparse
import os
import sys

from fin_analyst.config import load_settings
from fin_analyst.graph import run_analysis
from fin_analyst.market import fetch_quote
from fin_analyst.news import fetch_news
from fin_analyst.passages import fetch_passages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fin_analyst",
        description="Answer a question about a company with a memo built from its latest 10-K.",
    )
    parser.add_argument("ticker", help="stock ticker, e.g. MSFT")
    parser.add_argument("question", help='what you want to know, e.g. "How liquid is Microsoft?"')
    parser.add_argument(
        "--peer",
        metavar="TICKER",
        help="compare against another company, e.g. --peer GOOGL",
    )
    parser.add_argument(
        "--no-text",
        action="store_true",
        help="skip the filing's narrative: faster and cheaper, but the memo cannot say why",
    )
    parser.add_argument(
        "--news",
        action="store_true",
        help="also read the company's recent 8-K press releases",
    )
    parser.add_argument(
        "--news-index",
        action="store_true",
        help="add GDELT headlines to --news (noisy: mostly market commentary)",
    )
    parser.add_argument(
        "--market",
        action="store_true",
        help="fetch a share price so valuation ratios work (needs ALPHAVANTAGE_KEY)",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the claim check: cheaper, but nobody checks the citations hold",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        help="keep asking follow-ups about the same company (a few turns, one shared budget)",
    )
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

    print(f"Ticker:   {ticker}" + (f" vs {args.peer.upper()}" if args.peer else ""))
    print(f"Question: {args.question}")
    for name, node in settings.nodes.items():
        print(f"{name + ':':9s} {node.model}, effort {node.effort}, max {node.max_tokens} tokens")
    print(f"Stops if a run passes ${settings.max_usd_per_run:.2f}")
    if args.chat:
        print(f"Chat:     up to {settings.chat_max_turns} questions, sharing that budget")

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
    analyst = ClaudeAnalyst(settings)

    # None means "don't read the narrative at all"; the default reads Item 7 and 1A.
    fetch_text = None if args.no_text else fetch_passages

    if args.chat:
        return run_chat(ticker, args.question, analyst, settings, args.peer, fetch_text)

    state = run_analysis(
        ticker, args.question, analyst, settings, peer_ticker=args.peer,
        fetch_text=fetch_text, verify=not args.no_verify,
        quote=fetch_quote if args.market else None,
        fetch_news=(
            (lambda ticker: fetch_news(ticker, include_index=args.news_index))
            if (args.news or args.news_index)
            else None
        ),
    )
    show(state, analyst)
    return 0 if state.memo else 1


def show(state, analyst) -> None:
    if state.memo:
        print(state.memo)
    else:
        print(f"No memo: {state.error}", file=sys.stderr)
    # analyst.spent_usd counts rejected replies too; state.cost_usd only counts
    # the calls the workflow accepted.
    print(f"\n[{len(state.drafts)} draft(s), ${analyst.spent_usd:.4f} spent]", file=sys.stderr)


def run_chat(ticker, question, analyst, settings, peer=None, fetch_text=None) -> int:
    """Ask follow-ups until the turns or the budget run out, whichever comes first."""
    from fin_analyst.chat import ChatSession

    chat = ChatSession(ticker, analyst, settings, peer_ticker=peer, fetch_text=fetch_text)
    answered = 0

    while question:
        state = chat.ask(question)
        show(state, analyst)
        answered += state.memo is not None

        if chat.turns_left <= 0:
            print(f"\nThat was the last of {settings.chat_max_turns} questions.", file=sys.stderr)
            break
        if state.error and "over the" in state.error:
            break

        try:
            question = input(f"\n[{chat.turns_left} left] Ask another, or press enter to stop: ").strip()
        except EOFError:
            break

    print(f"\n[{answered} answer(s), ${analyst.spent_usd:.4f} spent in total]", file=sys.stderr)
    return 0 if answered else 1


if __name__ == "__main__":
    sys.exit(main())
