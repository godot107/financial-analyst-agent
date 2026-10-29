"""Which request features work for each Claude model on Amazon Bedrock? (AGENTCORE_PLAN.md, Phase 0)

config.yaml's `bedrock.models` says what each model supports (effort, strict tools); this
checks those claims with the smallest real calls, so a gold-set run doesn't discover them by
failing. Each call asks for a one-word answer.

    python scripts/bedrock_probe.py              # what it would call, and the most it can cost
    python scripts/bedrock_probe.py --yes        # make the calls (AWS credentials: aws login)

Spends money (well under $0.10 in all). Ask Willie before running it.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fin_analyst.config import load_settings  # noqa: E402

TOOL = {
    "name": "answer",
    "description": "Record the answer.",
    "input_schema": {
        "type": "object",
        "properties": {"word": {"type": "string", "enum": ["yes", "no"]}},
        "required": ["word"],
        "additionalProperties": False,
    },
}
ASK = [{"role": "user", "content": "Is water wet? Answer yes or no."}]
EFFORT_BETA = "effort-2025-11-24"

# (name, what it shows, extra request fields, betas, max_tokens)
CASES = [
    ("plain", "the model answers at all", {}, [], 16),
    ("effort", "the effort setting (llm.py, Opus 4.5)", {"output_config": {"effort": "low"}}, [EFFORT_BETA], 16),
    ("strict_forced_tool", "a strict tool forced, as plan and verify use",
     {"tools": [{**TOOL, "strict": True}], "tool_choice": {"type": "tool", "name": "answer"}}, [], 64),
    ("forced_tool", "the same tool without strict",
     {"tools": [TOOL], "tool_choice": {"type": "tool", "name": "answer"}}, [], 64),
    ("thinking_forced_tool", "manual thinking with a forced tool (expected to fail)",
     {"thinking": {"type": "enabled", "budget_tokens": 1024}, "tools": [TOOL],
      "tool_choice": {"type": "tool", "name": "answer"}}, [], 1200),
    ("adaptive_thinking", "adaptive thinking, as the Claude API path uses (expected to fail)",
     {"thinking": {"type": "adaptive"}}, [], 1200),
]


def most_it_can_cost(settings, models) -> float:
    total = 0.0
    for model in models:
        for *_, max_tokens in CASES:
            total += settings.cost_usd(model, 300, max_tokens)
        total += settings.cost_usd(model, 300, 16)  # the Converse call
    return total


def probe_messages(client, model_id, case):
    name, _, extra, betas, max_tokens = case
    request = dict(model=model_id, max_tokens=max_tokens, messages=ASK, **extra)
    if betas:
        return client.beta.messages.create(betas=betas, **request)
    return client.messages.create(**request)


def probe_converse(settings, model_id, effort: bool):
    """The Strands chat path: Bedrock's Converse API, the effort beta in additional fields."""
    import boto3

    runtime = boto3.client("bedrock-runtime", region_name=settings.bedrock_region)
    extra = {"anthropic_beta": [EFFORT_BETA], "output_config": {"effort": "low"}} if effort else {}
    response = runtime.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": ASK[0]["content"]}]}],
        inferenceConfig={"maxTokens": 16},
        **({"additionalModelRequestFields": extra} if extra else {}),
    )
    return response["usage"]["inputTokens"], response["usage"]["outputTokens"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--yes", action="store_true", help="make the calls (spends money)")
    parser.add_argument("--model", action="append", help="a bedrock.models name (default: all)")
    args = parser.parse_args(argv)

    import os

    os.environ["FIN_ANALYST_PROVIDER"] = "bedrock"
    settings = load_settings()
    models = args.model or sorted(settings.bedrock_models)
    ceiling = most_it_can_cost(settings, models)
    print(f"{len(models)} model(s) x {len(CASES) + 1} calls; at most ${ceiling:.3f}")
    if not args.yes:
        for model in models:
            print(f"  {model}: {settings.bedrock_model(model).invoke_id()}")
        print("Dry run. Add --yes to make the calls.")
        return 0

    import anthropic

    from fin_analyst.llm import make_client

    client = make_client(settings)
    results, spent = [], 0.0
    for model in models:
        spec = settings.bedrock_model(model)
        model_id = spec.invoke_id()
        for case in CASES:
            started = time.monotonic()
            try:
                response = probe_messages(client, model_id, case)
                cost = settings.cost_usd(model, response.usage.input_tokens, response.usage.output_tokens)
                outcome = f"ok ({response.stop_reason})"
            except anthropic.APIStatusError as failed:
                cost, outcome = 0.0, f"FAILED {failed.status_code}: {failed.message[:140]}"
            spent += cost
            results.append({"model": model, "case": case[0], "shows": case[1], "outcome": outcome,
                            "cost_usd": round(cost, 5), "seconds": round(time.monotonic() - started, 2)})
            print(f"{model:18} {case[0]:22} {outcome}")
        try:
            tokens_in, tokens_out = probe_converse(settings, model_id, spec.effort)
            cost = settings.cost_usd(model, tokens_in, tokens_out)
            outcome = "ok"
        except Exception as failed:  # botocore's ClientError: report it, don't stop the probe
            cost, outcome = 0.0, f"FAILED: {str(failed)[:140]}"
        spent += cost
        results.append({"model": model, "case": "converse", "shows": "the Strands chat path",
                        "outcome": outcome, "cost_usd": round(cost, 5)})
        print(f"{model:18} {'converse':22} {outcome}")

    out = Path("runs") / f"bedrock_probe_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"spent_usd": round(spent, 4), "results": results}, indent=2) + "\n")
    print(f"spent ${spent:.4f}; record: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
