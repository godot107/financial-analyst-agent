"""Pipeline chat vs Strands chat on the pre-registered follow-ups (evals/chat_followups.yaml).

    python evals/chat_compare.py                      # what would be asked and what it may spend, $0
    python evals/chat_compare.py --spend              # both arms, about $2.40; stops past --max-usd
    python evals/chat_compare.py --score runs/chat-compare-<stamp>   # after grading, $0

Each company's opening memo is made once by the pipeline and given to both arms as
turn 1, so the arms differ only in how they answer the follow-up:
  pipeline - ChatSession: the fixed workflow again, with the memo as history
  strands  - StrandsChatSession: an agent that picks its own tools (docs/STRANDS_PLAN.md)

--spend writes results.json (everything measured, plus which arm is A or B), and
grading.md + grades.yaml for blind grading: fill grades.yaml in from grading.md
without opening results.json, then run --score.
"""

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

FOLLOW_UPS = Path(__file__).resolve().parent / "chat_followups.yaml"
ARMS = ("pipeline", "strands")
# Guesses until the first run measures them. The agent resends the conversation
# and the tool results on every call, and makes several calls per question.
ESTIMATE_OPENING = 0.06
ESTIMATE = {"pipeline": 0.06, "strands": 0.12}


def load_cases(path: Path = FOLLOW_UPS) -> tuple[dict, list[dict]]:
    plan = yaml.safe_load(path.read_text())
    cases = [
        {**follow_up, "ticker": ticker, "opening": company["opening"]}
        for ticker, company in plan["companies"].items()
        for follow_up in company["follow_ups"]
    ]
    return plan, cases


def estimate(cases: list[dict]) -> float:
    openings = len({c["ticker"] for c in cases})
    return openings * ESTIMATE_OPENING + len(cases) * sum(ESTIMATE.values())


# --- measuring -----------------------------------------------------------------


def claude_calls(trace) -> int:
    return sum(1 for e in trace if e.step == "claude")


def pipeline_row(state, cost: float, seconds: float) -> dict:
    checks = state.claim_checks
    return {
        "answer": state.memo, "error": state.error, "drafts": len(state.drafts),
        "first_draft_pass": bool(state.memo) and len(state.drafts) == 1,
        "claims": len(checks), "claims_supported": sum(c.supported for c in checks),
        "cost_usd": round(cost, 5), "seconds": round(seconds, 1),
        "model_calls": claude_calls(state.trace), "tool_calls": 0,
    }


def strands_row(answer, seconds: float) -> dict:
    checks = answer.claim_checks
    return {
        "answer": answer.answer, "error": answer.error, "drafts": len(answer.drafts),
        "first_draft_pass": bool(answer.answer) and len(answer.drafts) == 1,
        "claims": len(checks), "claims_supported": sum(c.supported for c in checks),
        "cost_usd": answer.cost_usd, "seconds": round(seconds, 1),
        "model_calls": claude_calls(answer.trace), "tool_calls": len(answer.tool_calls),
        "tools": [c["tool"] + (" (cancelled)" if c["cancelled"] else "") for c in answer.tool_calls],
    }


# --- blind grading -------------------------------------------------------------


def blind(results: list[dict], seed: int) -> list[dict]:
    """Which arm is A for each case: shuffled, but reproducible from the seed."""
    rng = random.Random(seed)
    for row in results:
        order = list(ARMS)
        rng.shuffle(order)
        row["key"] = {"A": order[0], "B": order[1]}
    return results


def grading_sheet(results: list[dict], plan: dict) -> str:
    lines = ["# Chat comparison: blind grading", "", plan["grading"].strip(), "",
             "Fill in grades.yaml (0, 1 or 2 for A and B). Don't open results.json until you're done.", ""]
    for row in results:
        lines += [f"## {row['id']} ({row['ticker']})", "",
                  f"**Opening:** {row['opening']}  ", f"**Follow-up:** {row['question']}  ",
                  f"**A good answer:** {row['expect']}", ""]
        for label in ("A", "B"):
            arm = row[row["key"][label]]
            body = arm["answer"] or f"_(no answer: {arm['error']})_"
            lines += [f"### {label}", "", body, ""]
    return "\n".join(lines)


def grades_template(results: list[dict]) -> str:
    return "".join(f"{row['id']}:\n  A:\n  B:\n" for row in results)


def score(results: list[dict], grades: dict) -> dict:
    """Per arm: mean grade, first-draft pass rate, cost and effort per follow-up."""
    summary = {}
    for arm in ARMS:
        rows = [r for r in results if arm in r]
        marks = []
        for r in rows:
            label = next(k for k, v in r["key"].items() if v == arm)
            mark = (grades.get(r["id"]) or {}).get(label)
            if mark not in (0, 1, 2):
                raise ValueError(f"{r['id']} {label}: grade must be 0, 1 or 2, got {mark!r}")
            marks.append(mark)
        claims = sum(r[arm]["claims"] for r in rows)
        summary[arm] = {
            "questions": len(rows),
            "mean_grade": sum(marks) / len(rows),
            "first_draft_pass": sum(r[arm]["first_draft_pass"] for r in rows) / len(rows),
            "answered": sum(bool(r[arm]["answer"]) for r in rows) / len(rows),
            "cost_per_follow_up": sum(r[arm]["cost_usd"] for r in rows) / len(rows),
            "model_calls": sum(r[arm]["model_calls"] for r in rows) / len(rows),
            "tool_calls": sum(r[arm]["tool_calls"] for r in rows) / len(rows),
            "claims_supported": (sum(r[arm]["claims_supported"] for r in rows) / claims) if claims else None,
        }
    p, s = summary["pipeline"], summary["strands"]
    summary["verdict"] = {
        "grade_at_least_pipeline": s["mean_grade"] >= p["mean_grade"],
        "first_draft_no_lower": s["first_draft_pass"] >= p["first_draft_pass"],
        "cost_within_2x": s["cost_per_follow_up"] <= 2 * p["cost_per_follow_up"],
    }
    summary["verdict"]["strands_earns_a_place"] = all(summary["verdict"].values())
    return summary


def print_summary(summary: dict) -> None:
    rows = [("mean grade (0-2)", "mean_grade", "{:.2f}"), ("first-draft pass", "first_draft_pass", "{:.0%}"),
            ("answered", "answered", "{:.0%}"), ("cost per follow-up", "cost_per_follow_up", "${:.4f}"),
            ("model calls", "model_calls", "{:.1f}"), ("tool calls", "tool_calls", "{:.1f}"),
            ("claims supported", "claims_supported", "{:.0%}")]
    print(f"{'':22}{'pipeline':>12}{'strands':>12}")
    for label, key, fmt in rows:
        cells = [fmt.format(summary[a][key]) if summary[a][key] is not None else "-" for a in ARMS]
        print(f"{label:22}{cells[0]:>12}{cells[1]:>12}")
    print()
    for rule, held in summary["verdict"].items():
        print(f"  {'yes' if held else 'no ':3}  {rule.replace('_', ' ')}")


# --- the paid run --------------------------------------------------------------


def run(cases: list[dict], plan: dict, max_usd: float, out: Path) -> list[dict]:
    from fin_analyst.cache import LocalCache
    from fin_analyst.chat import ChatSession
    from fin_analyst.chat_tools import ChatToolbox
    from fin_analyst.config import load_settings
    from fin_analyst.edgar import describe_filing, fetch_facts
    from fin_analyst.llm import ClaudeAnalyst
    from fin_analyst.news import fetch_news
    from fin_analyst.passages import fetch_passages
    from fin_analyst.strands_chat import StrandsChatSession

    settings = load_settings()
    cache = LocalCache(ROOT / "cache")
    fetch = partial(fetch_facts, cache=cache)
    spent, results, openings = 0.0, [], {}

    for case in cases:
        if spent >= max_usd:
            print(f"\nStopped: ${spent:.4f} spent, past the ${max_usd:.2f} limit.")
            break
        ticker = case["ticker"]

        if ticker not in openings:
            print(f"\n{ticker} opening: {case['opening']}")
            analyst = ClaudeAnalyst(settings)
            opening = ChatSession(ticker, analyst, settings, fetch=fetch, runs_dir=out,
                                  fetch_text=partial(fetch_passages, cache=cache), describe=describe_filing)
            state = opening.ask(case["opening"])
            spent += analyst.spent_usd
            openings[ticker] = (opening, state, analyst.spent_usd)
            print(f"  {'memo' if state.memo else 'FAILED: ' + str(state.error)}, ${analyst.spent_usd:.4f}")
        opening, opening_state, opening_cost = openings[ticker]
        if not opening_state.memo:
            continue
        row = {**case, "opening_memo": opening_state.memo, "opening_cost_usd": round(opening_cost, 5)}
        print(f"\n{case['id']}: {case['question']}")

        # Pipeline: the same workflow again, with the memo as turn 1.
        analyst = ClaudeAnalyst(settings)
        chat = ChatSession(ticker, analyst, settings, fetch=lambda t: opening.facts, runs_dir=out,
                           fetch_text=lambda t: opening.passages, describe=describe_filing)
        chat.history = [(case["opening"], opening_state.memo)]
        chat.spent_usd = opening_cost
        started = time.monotonic()
        state = chat.ask(case["question"])
        row["pipeline"] = pipeline_row(state, analyst.spent_usd, time.monotonic() - started)
        spent += analyst.spent_usd

        # Strands: an agent with tools, starting from the same memo.
        verifier = ClaudeAnalyst(settings)
        toolbox = ChatToolbox(
            ticker, opening.facts, opening.passages,
            company=opening_state.filing.company if opening_state.filing else None,
            fetch_peer=fetch, fetch_news=lambda t: fetch_news(t, cache=cache),
        )
        agent = StrandsChatSession(toolbox, settings, verifier=verifier, runs_dir=out)
        verifier.tracer = agent.tracer  # its claim checks land in the agent's trace
        agent.seed(case["opening"], opening_state.memo, opening_cost)
        started = time.monotonic()
        try:
            answer = agent.ask(case["question"])
            row["strands"] = strands_row(answer, time.monotonic() - started)
        except Exception as failed:  # a crash is a result too, and must not lose the run
            row["strands"] = {"answer": None, "error": f"crashed: {failed}", "drafts": 0,
                              "first_draft_pass": False, "claims": 0, "claims_supported": 0,
                              "cost_usd": round(agent.spent_usd - opening_cost, 5), "seconds": 0,
                              "model_calls": 0, "tool_calls": 0, "tools": []}
        spent += row["strands"]["cost_usd"]

        for arm in ARMS:
            r = row[arm]
            print(f"  {arm:9} {'ok ' if r['answer'] else 'NO '} drafts {r['drafts']}  "
                  f"calls {r['model_calls']}  tools {r['tool_calls']}  ${r['cost_usd']:.4f}")
        results.append(row)
        save(out, results, plan)  # after every case, so a stop keeps what was done

    print(f"\n{len(results)} follow-ups, ${spent:.4f} spent in total.")
    return results


def save(out: Path, results: list[dict], plan: dict, seed: int = 20261027) -> None:
    out.mkdir(parents=True, exist_ok=True)
    blind(results, seed)
    (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    (out / "grading.md").write_text(grading_sheet(results, plan) + "\n")
    # Each --spend run gets its own folder, so this never overwrites finished grades.
    (out / "grades.yaml").write_text(grades_template(results))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spend", action="store_true", help="run both arms through Claude")
    parser.add_argument("--max-usd", type=float, default=3.0, help="stop starting cases past this (default 3.00)")
    parser.add_argument("--only", metavar="TICKER", help="just this company's follow-ups")
    parser.add_argument("--score", metavar="DIR", type=Path, help="score a graded run ($0)")
    args = parser.parse_args(argv)

    plan, cases = load_cases()
    cases = [c for c in cases if not args.only or c["ticker"] == args.only.upper()]

    if args.score:
        results = json.loads((args.score / "results.json").read_text())
        summary = score(results, yaml.safe_load((args.score / "grades.yaml").read_text()) or {})
        (args.score / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print_summary(summary)
        return 0

    if not args.spend:
        for case in cases:
            print(f"  {case['ticker']:5} {case['kind']:9} {case['question']}")
        print(f"\n{len({c['ticker'] for c in cases})} opening memos + {len(cases)} follow-ups x 2 arms, "
              f"about ${estimate(cases):.2f}.\nRun with --spend to ask them (stops starting cases past "
              f"${args.max_usd:.2f}; each conversation also stops past max_usd_per_run).")
        return 0

    load_dotenv(ROOT / ".env")
    out = ROOT / "runs" / f"chat-compare-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    run(cases, plan, args.max_usd, out)
    print(f"Blind grading sheet: {(out / 'grading.md').relative_to(ROOT)}; grades go in grades.yaml.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
