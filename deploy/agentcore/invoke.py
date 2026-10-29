"""One memo from the AgentCore Runtime (SigV4-signed InvokeAgentRuntime). SPENDS MONEY.

    .venv/bin/python deploy/agentcore/invoke.py MSFT "How liquid is Microsoft?"          # shows the cost, asks
    .venv/bin/python deploy/agentcore/invoke.py MSFT "How liquid is Microsoft?" --yes    # no prompt

A memo is estimated at a few cents (the service's own estimate is printed).
Ask Willie before running it.
"""

import argparse
import json
import sys
import uuid

STACK = "fin-analyst-agentcore-app"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ticker")
    parser.add_argument("question")
    parser.add_argument("--no-text", action="store_true", help="ratios only: cheaper, no claim check")
    parser.add_argument("--yes", action="store_true", help="don't ask before spending")
    args = parser.parse_args(argv)

    import boto3

    from fin_analyst.service import MemoRequest

    request = MemoRequest(ticker=args.ticker, question=args.question,
                          text=not args.no_text, verify=not args.no_text)
    estimate = request.estimate_usd()
    if not args.yes and input(f"Spend up to about ${estimate:.2f} on one memo? [y/N] ").lower() != "y":
        print("Nothing sent.")
        return 0

    outputs = boto3.client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]
    out = {o["OutputKey"]: o["OutputValue"] for o in outputs}
    response = boto3.client("bedrock-agentcore").invoke_agent_runtime(
        agentRuntimeArn=out["RuntimeArn"],
        qualifier=out["EndpointName"],
        runtimeSessionId=f"memo-{uuid.uuid4()}",  # 33+ characters; one session per memo
        # Naming a user makes Runtime hand the container a workload access token, which
        # AgentCore Identity exchanges for the Claude API key.
        runtimeUserId="cli",
        contentType="application/json",
        accept="application/json",
        payload=json.dumps({**request.model_dump(), "caller": "cli"}).encode(),
    )
    job = json.loads(response["response"].read())
    print(job.get("memo") or json.dumps(job, indent=2))
    print(f"\nstatus {job.get('status')}, cost ${job.get('cost_usd', 0):.4f}, job {job.get('id')}")
    return 0 if job.get("status") == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
