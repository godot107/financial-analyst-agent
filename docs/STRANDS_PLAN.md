# Plan: a Strands follow-up chat, measured against `--chat`

Status: **planned, not built.** Nothing here has been run or spent.

## The question

Today `--chat` answers each follow-up by running the whole fixed workflow again
(plan → compute → retrieve → write → check → verify), with the earlier answers as context.
That is predictable and cheap, but a follow-up like "what about debt?" or "any news since the
filing?" goes through the same steps whether it needs them or not.

[Strands Agents](https://strandsagents.com/) works the other way: the model is given tools
and decides which to call, in what order, and when to stop. The question this experiment
answers is:

> For open-ended follow-ups, does letting Claude choose its own tools give better answers,
> and what does it cost in money, tool calls and broken rules?

**The memo itself stays a fixed LangGraph pipeline.** Its fixed order is what makes the
tests, the gold set and the run records comparable from run to run. This plan only adds a
second way to answer follow-ups, next to the current one.

**Human-AI chat only.** A tool-choosing agent makes sense when a person is reading each
answer and asking the next question. Anything automated stays on the fixed pipeline: the HTTP
service and Lambda, `scripts/batch.py`, the gold set, and the AuraTrade advisory memos. No
program ever consumes a Strands answer, so `service.py`, `lambda_handler.py` and the
`requirements.txt` that builds the Lambda image don't change. `strands-agents` goes in a
separate `requirements-chat.txt`, the same way `requirements-notebook.txt` keeps jupyter
out of the image.

## What must not change

The house rules hold in the new mode exactly as in the old one:

- **Claude never writes a number.** Tools return values *for judgment* together with the
  placeholder names (`{{roe:2026}}`) the answer must use. `memo.find_problems` checks the
  final answer, and `memo.render` fills the numbers.
- **The same checks run on the answer:** leaked digits, rules of thumb, repeated values,
  citations. The `verify` claim check runs on cited claims.
- **Same budget guard:** `max_usd_per_run` covers the session, and `chat_max_turns` (5)
  still applies.
- **Tests never touch the network or the API.** The Strands agent runs against a fake
  model in tests.
- **LLM isolation:** the new module goes on the allow-list in `tests/test_llm_isolation.py`,
  and that test also starts guarding `import strands`.

## Design

A new flat module, `fin_analyst/strands_chat.py`, with a `StrandsChatSession` that
has the same `ask(question)` interface as `ChatSession`. A `--chat-mode strands` flag picks it;
the default stays `pipeline`.

### Tools (thin wrappers over code that already exists)

| Tool | Wraps | Returns |
|---|---|---|
| `list_metrics()` | metric registry | ids + definitions |
| `get_metrics(ids, years)` | `metrics.py` | values for judgment, placeholder names, unavailable reasons |
| `search_filing(query)` | `passages.py` (BM25) | numbered passages `[P1]..` |
| `peer_metrics(ticker, ids)` | the `--peer` path | values + `{{peer.metric:year}}` names |
| `recent_news()` | `news.py` | dated `[N1]..` passages |

The facts and passages come from the session's one fetch, the same as `ChatSession`, so the SEC
is hit once.

### Model settings (match `config.yaml`)

`strands.models.anthropic.AnthropicModel`: `model_id: claude-opus-5`, `max_tokens` from a
new `nodes.chat_agent` entry, and `params={"thinking": {"type": "adaptive"},
"output_config": {"effort": ...}}`. **Step 0 confirms** that `params` passes `output_config`
through (the docs show `thinking` but don't mention `output_config`) and how to keep refusal
fallbacks (the beta header). If either can't be done, record it as a finding, not a workaround.

### Hooks (Strands' place for the house rules)

- **Spend guard:** `AfterModelCallEvent` adds up the tokens × `prices_per_million_tokens` and
  stops the run once it passes the cap.
- **Tool-call cap:** a `BeforeToolCallEvent` hook sets `cancel_tool` past N calls per turn
  (the `LimitToolCounts` pattern from the Strands hooks docs).
- **Check and retry:** `AfterInvocationEvent` runs `find_problems` on the answer. If it finds
  problems, it sets `event.resume` to the list of problems, up to `max_retries` (2), the same
  as the pipeline's write → check loop.
- **Trace:** every hook writes into the run's `Tracer`, so a Strands run record looks like a
  pipeline one (tool calls instead of nodes). `result.metrics` (`accumulated_usage`,
  `cycle_durations`, `tool_metrics`) gives the totals.

## Evaluation (pre-registered before any paid run)

**Question set:** about 12 follow-ups across 3 companies (MSFT, COST, JPM), each as
turn 2 after the same opening memo. They come in three kinds: *narrow* ("what's the quick
ratio?"), *open* ("what about debt?"), and *off-plan* ("any news since the filing?",
"how does it compare with Target?"). The set is written down in `evals/chat_followups.yaml`
before either arm runs.

**Measured per answer, both arms:**

| Measure | How |
|---|---|
| Cost and latency | trace |
| Model calls and tool calls | trace |
| First-draft pass rate on `find_problems` | check |
| Claim-check support rate | `verify` node |
| Answers the question (0–2) | Willie grades blind, arms shuffled |

**Decision rule (written now):** Strands earns a place in the CLI only if it scores **at least
as well on answers** *and* **no worse on first-draft pass rate**, at **no more than 2× the cost**
per answer. Otherwise it stays as a written-up comparison, which is still the deliverable.

**Spend:** 12 questions × 2 arms at about $0.03–0.10 each is about **$1–2.50**. Ask Willie before
running, the same as every live run, with the free steps kept separate from the paid one.

## Steps

0. **Read the docs and run a spike (free).** Install `strands-agents[anthropic]` in the project venv
   and confirm the `params` pass-through, the fallback beta, the hook event fields and the
   `@tool` signature/return format against the installed version. Write the confirmed details
   here.
1. **Tools + fake-model tests (free).** The tools, with unit tests on fixtures. Strands
   driven by a fake model that calls tools in a scripted order.
2. **Hooks (free).** The spend guard, tool cap, check-and-retry and trace. Tests: a scripted
   model that leaks a digit gets resumed, and one that loops gets cut off.
3. **CLI flag + question set (free).** `--chat-mode strands`, `evals/chat_followups.yaml`, and
   a `--dry-run` that prints both arms' plans.
4. **Paid comparison (ask first).** Both arms on the question set.
5. **Write up.** Results table in the README, a section in BLOG-2 or a new post, and the
   decision as the rule above decides it.

Not in scope: changing the memo pipeline, any automated caller (API, Lambda, batch,
AuraTrade), the Strands multi-agent patterns (Graph/Swarm), and AgentCore.
