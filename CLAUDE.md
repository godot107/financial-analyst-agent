# financial-analyst-agent

A LangGraph workflow that answers a question about one company with a short memo built from its
latest 10-K. Claude writes the words; Python computes every number.
Trello #87: https://trello.com/c/O2wlT16g

**Status:** Iteration 1 complete; iteration 2 has peer comparison (`--peer`). 101 tests; a memo costs about $0.03. Results and limitations are in the README; the rest of iteration 2 is in `PLAN.md` §6. `PLAN.md` is the build spec. Work its steps in
order, and keep Iteration 1 small and easy to follow; save extras for Iteration 2.

## Build / run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # ANTHROPIC_API_KEY, SEC_USER_AGENT
pytest                            # no network, no API key needed
python -m fin_analyst MSFT "How liquid is Microsoft?"
python -m fin_analyst MSFT "How liquid is Microsoft?" --chat      # up to 5 follow-ups
python -m fin_analyst MSFT "Compare with Alphabet" --peer GOOGL   # two companies
python -m fin_analyst MSFT "..." --dry-run                        # spends nothing
```

Project-local `.venv`. Local only: no AWS or Bedrock.

## Rules

- **Claude never writes a number.** Metrics come from `metrics.py`, and the memo uses
  `{{metric:year}}` / `{{metric:year->year}}` placeholders that `memo.render` fills. A digit
  outside a placeholder fails the check.
- **Only `llm.py`, `graph.py` and `__main__.py` may import `anthropic` or `fin_analyst.llm`.**
  `tests/test_llm_isolation.py` enforces this.
- **The words have rules too** (`PLAN.md` §2): year-over-year framing, no business reasons in
  Iteration 1, no rules of thumb, about 250 words. Prose from memory is the remaining risk once
  numbers are placeholders.
- **A missing input gives `None` with a reason, never a guessed value.** The one exception:
  short-term investments, receivables and debt lines that aren't reported count as 0, labelled
  in the memo footer.
- **Ratio definitions follow the textbooks cited in `PLAN.md` Step 2.** Use parent-only net
  income and equity. Quick ratio = (cash + short-term investments + receivables) / current
  liabilities. Debt = the sum of the debt lines, never total liabilities.
- **Every run saves a JSON run record in `runs/`**, even when no memo is produced.
- **Never trust the filing's `fiscal_year` column** — derive the year from the period end date
  (`fiscal_year_of`). Late-August filers label last year's figures with this year.
- **Values that move between nodes are Pydantic models** (`Fact`, `MetricResult`, the graph state,
  and Claude's plan), so a bad value fails at the node that made it.
- **Tests never hit the network or the API.** Use fixtures in `tests/fixtures/` and a fake client.
- **Ask Willie before any live Claude run**, and state the estimated cost. The budget guard
  (`max_usd_per_run` in `config.yaml`) stays on.
- Model: `claude-opus-5` with adaptive thinking and refusal fallbacks. Switching to a cheaper
  model is Willie's call.
- Keep code plain and commented for a reader learning the pattern. Prefer a flat module over a
  new abstraction.

## Key decisions

- **LangGraph for orchestration, the `anthropic` SDK for model calls** (no `langchain-anthropic`).
  The graph is the readable part; the raw SDK keeps each call explicit.
- **Pre-written ratio functions, not model-generated code.**
- **edgartools / XBRL rather than PDF parsing.**
- Real public filings only. Not investment advice.

## Related

- `../congress-signal`: LLM-isolation test pattern, `SEC_USER_AGENT`
- `../textbook-kb`: ratio definitions (Berk & DeMarzo Ch. 2); retrieval stack for Iteration 2
