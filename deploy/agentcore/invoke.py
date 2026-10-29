"""One memo from the AgentCore Runtime (SigV4-signed InvokeAgentRuntime). SPENDS MONEY.

    .venv/bin/python deploy/agentcore/invoke.py MSFT "How liquid is Microsoft?"          # shows the cost, asks
    .venv/bin/python deploy/agentcore/invoke.py MSFT "How liquid is Microsoft?" --yes    # no prompt

A memo is estimated at a few cents (the service's own estimate is printed).
Ask Willie before running it.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the project root, for fin_analyst


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ticker")
    parser.add_argument("question")
    parser.add_argument("--no-text", action="store_true", help="ratios only: cheaper, no claim check")
    parser.add_argument("--yes", action="store_true", help="don't ask before spending")
    args = parser.parse_args(argv)

    from fin_analyst.agentcore_client import invoke_memo
    from fin_analyst.service import MemoRequest

    request = MemoRequest(ticker=args.ticker, question=args.question,
                          text=not args.no_text, verify=not args.no_text)
    estimate = request.estimate_usd()
    if not args.yes and input(f"Spend up to about ${estimate:.2f} on one memo? [y/N] ").lower() != "y":
        print("Nothing sent.")
        return 0

    job = invoke_memo(request.model_dump(), caller="cli")
    print(job.get("memo") or json.dumps(job, indent=2))
    print(f"\nstatus {job.get('status')}, cost ${job.get('cost_usd', 0):.4f}, job {job.get('id')}")
    return 0 if job.get("status") == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
