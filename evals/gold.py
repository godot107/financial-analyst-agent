"""Grade the analyst against a hand-checked gold set (evals/gold.yaml).

Two levels, because they cost very different amounts:

    python evals/gold.py --free     # figures and ratios against the filings' own text. SEC data only, $0
    python evals/gold.py            # show what the full run would ask and what it may spend
    python evals/gold.py --spend    # the full workflow for every question, about $0.05 each
    python evals/gold.py --spend --runtime   # the same, each memo through AgentCore Runtime
    python evals/gold.py --spend --lambda    # the same, each memo through the Lambda stack

The free level checks what Python is responsible for: that each line item came
from the right tag, and that each ratio is the textbook formula over those
figures. The full level checks what Claude is responsible for: choosing ratios
that answer the question, and a memo that passes every check, says what it must
and doesn't say what it mustn't.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from fin_analyst.cache import LocalCache  # noqa: E402
from fin_analyst.edgar import fetch_facts  # noqa: E402
from fin_analyst.metrics import METRICS_BY_ID, compute_all  # noqa: E402

GOLD = Path(__file__).resolve().parent / "gold.yaml"
ESTIMATE_PER_QUESTION = 0.06


def load_gold(path: Path = GOLD) -> dict:
    return yaml.safe_load(path.read_text())


def check_figures(gold: dict, fetch) -> list[tuple[str, bool, str]]:
    """(what, passed, detail) for every gold fact, ratio and expected refusal."""
    rows = []
    for ticker, company in gold["companies"].items():
        facts = fetch(ticker)
        year = company["year"]
        accessions = {f.accession for f in facts}
        rows.append((f"{ticker} filing", company["accession"] in accessions,
                     f"expected {company['accession']}, got {sorted(accessions)}"))
        found = {f.line_item: f for f in facts if f.fiscal_year == year}

        for item, expected in company["facts"].items():
            fact = found.get(item)
            got = fact.value / 1e6 if fact else None
            ok = got is not None and abs(got - expected["value"]) < 0.5
            rows.append((f"{ticker} {item} {year}", ok, f"filing says {expected['value']:,}, got {got}"))

        results = {(r.metric_id, r.fiscal_year): r for r in compute_all(facts)}
        for metric_id, why in (company.get("unavailable") or {}).items():
            result = next((r for (m, _), r in sorted(results.items(), key=lambda kv: -kv[0][1])
                           if m == metric_id), None)
            ok = result is not None and result.value is None and why in (result.reason or "")
            rows.append((f"{ticker} {metric_id} unavailable", ok,
                         f"expected a reason mentioning '{why}', got {result.reason if result else 'no result'}"))

    for metric_id, tickers in gold["ratios"].items():
        metric = METRICS_BY_ID[metric_id]
        for ticker in tickers:
            company = gold["companies"][ticker]
            values = {k: v["value"] * 1e6 for k, v in company["facts"].items()}
            # Lines the filing doesn't have count as zero, exactly as in edgar.py.
            numbers = {name: values.get(name, 0.0) for name in metric.inputs}
            expected = metric.formula(numbers)
            result = compute_all(fetch(ticker), [metric_id])
            got = next((r.value for r in result if r.fiscal_year == company["year"]), None)
            ok = got is not None and abs(got - expected) <= 1e-6 * abs(expected)
            rows.append((f"{ticker} {metric_id} {company['year']}", ok,
                         f"from the filing's figures {expected:.4f}, computed {got}"))
    return rows


def grade_question(case: dict, state) -> list[tuple[str, bool, str]]:
    """What a finished run got right, for one question."""
    memo = state.memo or ""
    rows = [("memo published", bool(state.memo), state.error or f"{len(state.drafts)} draft(s)")]
    for group in case.get("choose_any", []):
        rows.append((f"chose one of {group}", bool(set(group) & set(state.metric_ids)),
                     f"chose {state.metric_ids}"))
    if case.get("memo_includes_any"):
        wanted = case["memo_includes_any"]
        rows.append((f"memo says one of {wanted}", any(w in memo for w in wanted), ""))
    for unwanted in case.get("memo_excludes", []):
        rows.append((f"memo avoids '{unwanted}'", unwanted not in memo, ""))
    if case.get("min_years"):
        years = {m.fiscal_year for m in state.metrics if m.value is not None}
        rows.append((f"at least {case['min_years']} years", len(years) >= case["min_years"], f"{sorted(years)}"))
    rows.append(("first draft passed", len(state.drafts) == 1, f"{len(state.drafts)} draft(s)"))
    return rows


def print_rows(rows) -> int:
    passed = 0
    for what, ok, detail in rows:
        passed += ok
        print(f"  {'pass' if ok else 'FAIL':4}  {what}" + ("" if ok or not detail else f"  -- {detail}"))
    return passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--free", action="store_true", help="check figures and ratios only ($0)")
    parser.add_argument("--spend", action="store_true", help="run every question through Claude")
    parser.add_argument("--max-usd", type=float, default=1.0, help="stop the full run past this (default 1.00)")
    parser.add_argument("--only", metavar="TICKER", help="just this company's questions")
    parser.add_argument("--runtime", action="store_true",
                        help="send each question to the AgentCore Runtime instead of running it here")
    parser.add_argument("--lambda", dest="lambda_", action="store_true",
                        help="send each question to the Lambda stack's function URL instead")
    parser.add_argument("--first", type=int, metavar="N", help="only the first N questions")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    gold = load_gold()
    cache = LocalCache(ROOT / "cache")

    if args.free:
        rows = check_figures(gold, partial(fetch_facts, cache=cache))
        passed = print_rows(rows)
        print(f"\n{passed}/{len(rows)} figure checks passed.")
        return 0 if passed == len(rows) else 1

    questions = [q for q in gold["questions"] if not args.only or q["ticker"] == args.only.upper()]
    if args.first:
        questions = questions[: args.first]
    if not args.spend:
        for case in questions:
            print(f"  {case['ticker']:5} {case['question']}")
        print(f"\n{len(questions)} questions, about ${len(questions) * ESTIMATE_PER_QUESTION:.2f}. "
              f"Run with --spend to ask them (stops past ${args.max_usd:.2f}).")
        return 0

    if args.runtime:
        return run_on_runtime(questions, args.max_usd)
    if args.lambda_:
        return run_on_runtime(questions, args.max_usd, invoke=lambda_invoker(), label="lambda")

    from fin_analyst.config import load_settings
    from fin_analyst.edgar import describe_filing
    from fin_analyst.graph import run_analysis
    from fin_analyst.llm import ClaudeAnalyst
    from fin_analyst.passages import fetch_passages

    settings = load_settings()
    out = ROOT / "runs" / f"gold-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    spent, all_rows, record = 0.0, [], []
    for case in questions:
        if spent >= args.max_usd:
            print(f"\nStopped: ${spent:.4f} spent, over the ${args.max_usd:.2f} limit.")
            break
        print(f"\n{case['ticker']}: {case['question']}")
        analyst = ClaudeAnalyst(settings)
        state = run_analysis(
            case["ticker"], case["question"], analyst, settings,
            fetch=partial(fetch_facts, cache=cache, filings=case.get("filings", 1)),
            fetch_text=partial(fetch_passages, cache=cache),
            describe=describe_filing, runs_dir=out,
        )
        spent += analyst.spent_usd
        rows = grade_question(case, state)
        print_rows(rows)
        print(f"  ${analyst.spent_usd:.4f}")
        all_rows += rows
        record.append({**case, "cost_usd": analyst.spent_usd, "drafts": len(state.drafts),
                       "grades": [{"check": w, "passed": ok, "detail": d} for w, ok, d in rows]})

    passed = sum(ok for _, ok, _ in all_rows)
    print(f"\n{passed}/{len(all_rows)} checks passed across {len(record)} questions, ${spent:.4f}.")
    out.mkdir(parents=True, exist_ok=True)
    (out / "gold.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"Memos, run records and grades: {out.relative_to(ROOT)}")
    return 0 if passed == len(all_rows) else 1


def lambda_invoker(poll_seconds: float = 0.25, limit: float = 600.0):
    """Submit to the Lambda stack and poll until the job finishes, as its callers do.
    Signed and keyed exactly as deploy/call.py does (it reads FA_URL, FA_API_KEY, FA_REGION)."""
    import importlib.util
    import os

    from dotenv import dotenv_values

    import boto3

    if "FA_URL" not in os.environ:
        outputs = boto3.client("cloudformation").describe_stacks(StackName="fin-analyst-app")["Stacks"][0]["Outputs"]
        os.environ["FA_URL"] = next(o["OutputValue"] for o in outputs if o["OutputKey"] == "FunctionUrl")
    if "FA_API_KEY" not in os.environ:  # the first key in .env, as call.sh picks it
        first = (dotenv_values(ROOT / ".env").get("FIN_ANALYST_API_KEYS") or "").split(",")[0]
        os.environ["FA_API_KEY"] = first.split("=", 1)[-1]
    os.environ.setdefault("FA_REGION", "us-east-1")
    spec = importlib.util.spec_from_file_location("deploy_call", ROOT / "deploy" / "call.py")
    call = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(call)

    def invoke(request: dict) -> dict:
        # A submission is never retried: if it went through, a retry would pay twice.
        try:
            status, accepted = call.call("POST", "/v1/memos", request)
        except OSError as failed:  # urllib's URLError and socket timeouts
            return {"status": "failed", "error": f"submission not confirmed: {failed}"}
        if status != 202:
            return {"status": "failed", "error": f"HTTP {status}: {accepted}"}
        started = time.monotonic()
        while time.monotonic() - started < limit:
            try:  # a poll is free and safe to repeat; a dropped one just waits for the next
                status, job = call.call("GET", accepted["status_url"])
            except OSError:
                status, job = None, None
            if status == 200 and job["status"] in ("done", "failed"):
                return job
            time.sleep(poll_seconds)
        return {"status": "failed", "error": f"still running after {limit:.0f}s"}

    return invoke


def run_on_runtime(questions: list[dict], max_usd: float, invoke=None, label: str = "runtime") -> int:
    """The paid level through a deployed service (AgentCore Runtime, or the Lambda stack
    with label="lambda"): same questions, same grader, the memo written in the cloud.
    Run records stay in the service's jobs bucket; grades and timings come here."""
    from fin_analyst.agentcore_client import as_state, graph_seconds, invoke_memo, new_session

    if invoke is None:
        session = new_session("gold")  # one session for the run: a warm container and key
        invoke = lambda request: invoke_memo(request, caller="gold", session=session)  # noqa: E731
    out = ROOT / "runs" / f"gold-{label}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    spent, all_rows, record = 0.0, [], []
    for case in questions:
        if spent >= max_usd:
            print(f"\nStopped: ${spent:.4f} spent, over the ${max_usd:.2f} limit.")
            break
        print(f"\n{case['ticker']}: {case['question']}")
        started = time.monotonic()
        job = invoke({"ticker": case["ticker"], "question": case["question"],
                      "filings": case.get("filings", 1)})
        seconds = round(time.monotonic() - started, 1)  # submit -> finished memo, as a caller sees it
        graph = graph_seconds(job)
        # The platform's share: everything the caller waited for that wasn't the graph running.
        overhead = round(seconds - graph, 2) if graph is not None else None
        cost = float(job.get("cost_usd") or 0.0)
        spent += cost
        rows = grade_question(case, as_state(job))
        print_rows(rows)
        print(f"  ${cost:.4f}  {seconds}s (overhead {overhead}s)  job {job.get('id')}")
        all_rows += rows
        record.append({**case, "cost_usd": cost, "seconds": seconds, "overhead_seconds": overhead,
                       "job": job.get("id"), "drafts": (job.get("result") or {}).get("drafts"),
                       "memo": job.get("memo"),
                       "grades": [{"check": w, "passed": ok, "detail": d} for w, ok, d in rows]})

    passed = sum(ok for _, ok, _ in all_rows)
    print(f"\n{passed}/{len(all_rows)} checks passed across {len(record)} questions, ${spent:.4f}.")
    times = sorted(r["seconds"] for r in record)
    if times:
        print(f"Seconds per memo: p50 {times[len(times) // 2]}, max {times[-1]} (first call includes any cold start).")
    overheads = sorted(r["overhead_seconds"] for r in record if r["overhead_seconds"] is not None)
    if overheads:
        import statistics

        print(f"Platform overhead (caller's wait minus the graph): median {statistics.median(overheads):.2f}s, "
              f"all {overheads}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "gold.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"Grades and memos: {out.relative_to(ROOT)}")
    return 0 if passed == len(all_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
