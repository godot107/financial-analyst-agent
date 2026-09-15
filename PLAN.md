# PLAN — Agentic Financial Analyst

Build spec for Claude Code. **Iteration 1 is deliberately small:** one company, one 10-K, a
handful of ratios, and a six-node LangGraph workflow you can read top to bottom in an afternoon.
Work the steps in order, and meet each step's "done when" before starting the next.

## 1. What it does

```bash
python -m fin_analyst MSFT "How liquid is Microsoft, and what drives its return on equity?"
```

That command produces a short markdown memo answering the question from Microsoft's latest 10-K.
Iteration 1 uses SEC filings only, with no web or news search (that comes in Iteration 2, §6).
**Every number in the memo comes from the filing via Python. Claude never types a number.**

This is not investment advice: no price targets, no buy/sell calls.

## 2. The one rule

**Claude writes the words, Python writes the numbers.**

- Python pulls facts from the filing and computes the ratios.
- Claude writes the memo using **placeholders** instead of numbers:
  - `{{current_ratio:2025}}` renders as "1.35x"
  - `{{net_margin:2024->2025}}` renders as "rose 1.2 pts to 36.1%"
- A check step rejects any draft containing a digit outside a placeholder. The only exceptions are
  fiscal years that appear in the data, and "10-K".
- If an input is missing, the ratio is "not available (reason)". Never guess or fill in a number.

**Why:** Claude has seen big companies' financials in training. Without this rule, a memo that
looks right might be memory instead of the filing. Step 6 tests this directly.

## 3. The workflow (LangGraph)

```
START → plan → fetch → compute → write → check ──ok──→ render → END
                                   ▲        │
                                   └─retry──┤ (up to 2 retries)
                                            └──gave up──→ END (no memo; problems listed)
```

| Node | Who | What it does |
|---|---|---|
| `plan` | Claude | Picks which metrics answer the question, from a fixed list |
| `fetch` | Python | Downloads the latest 10-K's financial statements into a list of facts |
| `compute` | Python | Calculates the chosen metrics from those facts |
| `write` | Claude | Drafts the memo with placeholders; on retry, sees the problems found |
| `check` | Python | Finds leaked digits and unknown placeholders |
| `render` | Python | Swaps placeholders for formatted values and saves the memo |

**Orchestration is LangGraph; the model calls use the `anthropic` SDK directly** (no
`langchain-anthropic`). LangGraph provides the visible graph and the retry loop. The raw SDK keeps
every model call explicit.

### State

```python
class AnalysisState(TypedDict, total=False):
    ticker: str
    question: str
    metric_ids: list[str]          # plan
    facts: list[Fact]              # fetch
    metrics: list[MetricResult]    # compute
    draft: str                     # write (contains placeholders)
    problems: list[str]            # check (empty = passed)
    attempts: int                  # write attempts so far
    memo: str                      # render
    cost_usd: float                # running total; the budget guard reads it
```

`Fact` = line item, fiscal year, value, XBRL concept tag, accession number.
`MetricResult` = metric id, fiscal year, value (or `None`), reason if `None`, inputs used.

## 4. Files

```
financial-analyst-agent/
├── fin_analyst/
│   ├── __main__.py   # CLI
│   ├── config.py     # config.yaml + .env
│   ├── edgar.py      # Step 1: 10-K → facts            (no LLM)
│   ├── metrics.py    # Step 2: ratio functions          (no LLM)
│   ├── memo.py       # Step 3: placeholders + checks    (no LLM)
│   ├── llm.py        # Step 5: the only file that imports anthropic
│   └── graph.py      # Step 4: LangGraph nodes + wiring
├── tests/
│   └── fixtures/     # recorded 10-K facts, so tests need no network
├── config.yaml       # model, budget, retries, prices
└── runs/             # saved memos (gitignored)
```

`tests/test_llm_isolation.py` fails if any file other than `llm.py`, `graph.py` or `__main__.py`
imports `anthropic` or `fin_analyst.llm`.

## 5. Steps

### Step 0 — Scaffold
Venv, pinned requirements, `config.yaml`, CLI skeleton, isolation test.
**Done when:** `pytest` passes and `python -m fin_analyst --help` works.

### Step 1 — Fetch facts from one 10-K (`edgar.py`)
- Ticker → latest 10-K → balance sheet, income statement and cash flow → `list[Fact]`, using
  edgartools (`set_identity`, `Company`, `get_filings(form="10-K")`, `filing.xbrl()`,
  `xbrl.statements...`). Check the exact calls against current edgartools docs.
- About 12 line items: revenue, gross profit, operating income, net income, total assets,
  current assets, cash, inventory, total liabilities, current liabilities, total equity,
  operating cash flow, capex, and total debt.
- XBRL tag names vary by company, so keep a small dict of candidate tags per line item in
  `edgar.py`. Take the first match, record which tag was used, and leave the line item out if
  nothing matches.
- Save MSFT's facts as a JSON fixture.

**Done when:** assets = liabilities + equity (within 0.5%) for each year in the fixture, and every
fact has an accession number.

### Step 2 — Metrics (`metrics.py`)
Eight metrics, using **ending** balances (simpler; note it in the memo footer):

| id | formula |
|---|---|
| `current_ratio` | current assets / current liabilities |
| `quick_ratio` | (current assets − inventory) / current liabilities |
| `debt_to_equity` | total debt / total equity |
| `gross_margin` | gross profit / revenue |
| `net_margin` | net income / revenue |
| `asset_turnover` | revenue / total assets |
| `equity_multiplier` | total assets / total equity |
| `roe` | net income / total equity (= net margin × asset turnover × equity multiplier, the DuPont identity; Berk & DeMarzo Ch. 2) |

Each metric is a small function plus a one-line description. The description is what `plan` shows
Claude.

**Done when:** tests pass for:
- values checked by hand against the MSFT 10-K
- DuPont product equals ROE
- a missing input gives `None` with a reason
- a zero denominator gives `None` with a reason

### Step 3 — Placeholders and checks (`memo.py`)
- `find_problems(draft, metrics, years) -> list[str]` covers leaked digits and unknown metric ids
  or years.
- `render(draft, metrics) -> str` formats values: ratios as `1.35x`, margins as `36.1%`, changes
  with "rose"/"fell"/"was unchanged".

**Done when:** tests cover a clean draft, a leaked number, a leaked percentage, an unknown
placeholder, an allowed year, the rendered direction words, and a `None` metric.

### Step 4 — The graph, with a fake Claude (`graph.py`)
- Wire the six nodes. The Claude-using nodes get their model client passed in, so tests can pass
  a fake one that returns scripted replies.
- **Done when:** tests cover:
  - happy path → memo
  - leaky draft → retry → clean draft → memo
  - three leaky drafts → gives up, no memo
  - `plan` choosing an unknown metric → error

### Step 5 — Real Claude (`llm.py`)
- `plan`: one call with a strict tool whose `metric_ids` is an enum of the metric list.
- `write`: one call returning the draft text. The system prompt explains the placeholder rule and
  lists the available placeholders. On retry, the prompt includes the problems found.
- Model `claude-opus-5`, adaptive thinking, and refusal fallbacks. Check `stop_reason` before
  reading.
- Budget guard: after each call, add the cost from `usage` using the prices in `config.yaml`, and
  stop if it passes `max_usd_per_run` ($1.00).
- **Before the first live run:** ask Willie, stating the estimated cost.

**Done when:** one live MSFT memo is saved in `runs/`, and the real cost is written in the README.

### Step 6 — Prove it
- **Perturbation test:** edit the MSFT fixture (e.g. halve net income), run the workflow on it,
  and confirm the memo shows the edited numbers, not the real ones Claude may remember.
- Run a second company to see what breaks in the tag mapping, and fix or document it.
- README: how it works, one example memo, cost per memo, limitations.

**Done when:** both results are in the README.

## 6. Iteration 2 (later, only after Step 6)

Pick from these based on what Iteration 1 taught:
- Compare two companies, and handle different fiscal year-ends
- Multiple years across several filings, with restatement handling
- Cite MD&A and risk-factor passages (retrieval, reusing the textbook-kb stack), plus a
  claim-support check
- **Recent news and commentary**, as one new `news` node between `compute` and `write`. Add the
  sources in this order:
  1. 8-K earnings press releases (Exhibit 99.1) via edgartools: free, published by the company,
     same library as `fetch`
  2. A news index, chosen by `news_source` in `config.yaml` behind one shared interface:

     | | NewsAPI.org free plan (`newsapi`) | GDELT (`gdelt`) |
     |---|---|---|
     | Use it for | Local runs | Any hosted or live demo |
     | Cost | $0 (paid plans start at $449/month) | $0 |
     | Terms | "development and testing in a development environment only… cannot be used in a staging or production environment (including internally)". There's no exception for personal or non-commercial projects. | "unlimited and unrestricted use for any academic, commercial, or governmental use" |
     | Obligations | Don't republish article text | Cite the GDELT Project and link gdeltproject.org in the memo footer |
     | Returns | Headline, description, URL | Headline, URL, topic/tone data |
     | Limits | 100 requests/day, 24-hour delay, 1-month history | — |
     | Setup | `NEWSAPI_KEY` in `.env` | No key |

     Terms for both were checked 2026-09-15. Neither returns full article text, so claims must
     stick to what the headline or description says. Record one response from each as a test
     fixture.
  3. Claude's server-side web search tool, limited to chosen domains (e.g. company IR pages,
     Reuters), if 1 and 2 fall short. It's billed per search on top of tokens, so check pricing
     first.

  Rules for anything from news:
  - It supplies words only, never numbers; the existing digit check already enforces this.
  - Every claim carries a source link and publication date.
  - The memo shows news in its own section, labelled with its dates, so it isn't mixed up with the
    10-K's fiscal period.
- A per-run trace file and more evals (a hand-checked gold set, claim grading)
- Averages instead of ending balances; solvency and cash-flow metrics
- Banks and insurers (current ratio doesn't apply)
- Publish: public repo, blog post

## 7. Known traps

- **XBRL values are in raw units, not millions.** Format at render time only.
- **Fiscal years differ** (MSFT's ends in June). Label memos with the period-end date.
- **SEC needs an identity:** set `SEC_USER_AGENT`, or requests get 403s.
- **Banks** have no current assets or liabilities, so the metric returns `None`, which is correct.
