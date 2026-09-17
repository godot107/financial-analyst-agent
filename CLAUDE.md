# financial-analyst-agent

A LangGraph workflow that answers a question about one company with a short memo built from its
latest 10-K. Claude writes the words; Python computes every number.
Trello #87: https://trello.com/c/O2wlT16g

**Status:** Iteration 1 complete; iteration 2 has peer comparison (`--peer`), MD&A citations (BM25 over Item 7/1A) a claim check (`verify` node), valuation ratios (`--market`) and 8-K news (`--news`). Iteration 2 is built; iteration 3 Phase A (HTTP service) is built and Phase B (Lambda, `infra/`, `deploy/`) is deployed (fin-analyst-app, us-east-1) — see `docs/API_PLAN.md`; calling it and reading traces is `docs/CALLING.md`. 289 tests; `python evals/gold.py --free` (77 checks, $0) before a deploy; a memo costs $0.03–0.09. Results and limitations are in the README; what's left is in `PLAN.md` §6. `PLAN.md` is the build spec. Work its steps in
order, and keep Iteration 1 small and easy to follow; save extras for Iteration 2.

## Build / run

Run everything from the project root (the folder holding `fin_analyst/`), inside the venv.
`python3 -m fin_analyst` with system Python fails on imports; from inside `fin_analyst/` it
fails to find the package at all.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # ANTHROPIC_API_KEY, SEC_USER_AGENT
pytest                            # no network, no API key needed
python -m fin_analyst MSFT "How liquid is Microsoft?"
python -m fin_analyst MSFT "How liquid is Microsoft?" --chat      # up to 5 follow-ups
python -m fin_analyst MSFT "Compare with Alphabet" --peer GOOGL   # two companies
python -m fin_analyst MSFT "Why did margins move?" --no-text      # ratios only, no narrative
python scripts/coverage.py MSFT COST            # which tags matched; free to run
python -m fin_analyst.server                    # HTTP service; needs FIN_ANALYST_API_KEYS
python scripts/batch.py "How liquid is it?" MSFT COST --dry-run
python -m fin_analyst MSFT "..." --dry-run                        # spends nothing
```

Project-local `.venv`. Runs locally, or on AWS Lambda via `deploy/` (no Bedrock: the Claude API directly).

## Rules

- **Claude never writes a number.** Metrics come from `metrics.py`, and the memo uses
  `{{metric:year}}` / `{{metric:year->year}}` placeholders that `memo.render` fills. A digit
  outside a placeholder fails the check.
- **Only `llm.py`, `graph.py`, `__main__.py`, `server.py` and `lambda_handler.py` may import `anthropic` or `fin_analyst.llm`.**
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
- **The API result is data, not just Markdown:** filing details, metrics with their inputs,
  passages marked cited, claim checks, trace (`service.structured_result`). A program reads
  those; nobody should parse the memo.
- **Every run saves a JSON run record in `runs/`**, even when no memo is produced. It includes the
  trace (`fin_analyst/trace.py`): each node's output and timing, and each Claude call's tokens, cost
  and summarized thinking. A sink that fails must never fail the run.
- **Never trust the filing's `fiscal_year` column** — derive the year from the period end date
  (`fiscal_year_of`). Late-August filers label last year's figures with this year.
- **Values that move between nodes are Pydantic models** (`Fact`, `MetricResult`, the graph state,
  and Claude's plan), so a bad value fails at the node that made it.
- **Tests never hit the network or the API.** Use fixtures in `tests/fixtures/` and a fake client.
  `tests/conftest.py` enforces it: any socket connection in a test raises. A worker given a real
  cache once looked up filings over the network while every test still passed.
- **Bump `EXTRACTION_VERSION` in `edgar.py` when line items or tags change.** Cached facts are
  keyed by accession *and* that version; without the bump, cached filings answer without the new lines.
- **Cache only what a key pins exactly** (`fin_analyst/cache.py`): filings by accession number,
  prices by day, memos by a hash that includes a fingerprint of the memo-writing code. Never cache
  "the latest filing for a ticker" — look that up every time.
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

## Deploying (Phase B)

`deploy/00_iam.sh` runs as **admin** (Willie's `default` profile, signed in with `aws login`);
everything after it runs as the restricted `fin-analyst` profile. Never use the `tictactoe` profile:
its policy covers Lightsail only. Never print a secret: keys go into the CLI credentials file or SSM
on stdin. `04_app.sh` without `--execute` only previews; ask before `--execute`, before
`05_verify.sh --memo` (it spends), and before any teardown.
