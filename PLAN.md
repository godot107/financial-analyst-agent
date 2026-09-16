# PLAN — Agentic Financial Analyst

Build spec for Claude Code. **Iteration 1 is deliberately small:** one company, one 10-K, nine
ratios, and a six-node LangGraph workflow you can read top to bottom in an afternoon. Work the
steps in order, and meet each step's "done when" before starting the next.

Reviewed against textbook-kb on 2026-09-15. Sources are cited inline:
- **B&D:** Berk & DeMarzo, *Corporate Finance*
- **Subramanyam:** *Financial Statement Analysis*
- **Huyen:** *AI Engineering*
- **Oshin:** *Learning LangChain*

## 1. What it does

```bash
python -m fin_analyst MSFT "How liquid is Microsoft, and what drives its return on equity?"
```

That command produces a short markdown memo answering the question from Microsoft's latest 10-K.
Iteration 1 uses SEC filings only, with no web or news search (that comes in Iteration 2, §6).
**Every number in the memo comes from the filing via Python. Claude never types a number.**

This is not investment advice: no price targets, no buy/sell calls.

## 2. The rules

**Claude writes the words, Python writes the numbers.**

- Python pulls facts from the filing and computes the ratios.
- Claude writes the memo using **placeholders** instead of numbers:
  - `{{current_ratio:2025}}` renders as "1.35x"
  - `{{net_margin:2024->2025}}` renders as "rose 1.2 pts to 36.1%"
- A check step rejects any draft containing a digit outside a placeholder. The only exceptions are
  fiscal years that appear in the data, and "10-K".
- If an input is missing, the ratio is "not available (reason)". Never guess or fill in a number.

**The words have rules too.** Placeholders make the numbers safe by construction, so the remaining
risk that Claude writes from memory is in the prose: "margins expanded", "driven by cloud growth".
Claude has likely seen public companies' financials in training (Huyen Ch. 4). The writer must:
- **Frame findings as year-over-year changes**, and say there is no peer comparison. Ratios
  aren't meaningful in isolation, only against prior years, standards or competitors
  (Subramanyam Ch. 1), and Iteration 1 only has prior years.
- **Explain only what the numbers show**, e.g. which DuPont component moved. Iteration 1 has no
  text from the filing, so any business reason would come from memory.
- **Skip rules of thumb** ("a current ratio above 2 is healthy"). They aren't in the data, and
  their digits fail the check anyway.
- **Keep it short:** about 250 words. Shorter answers give fewer chances to hallucinate (Huyen
  Ch. 2).

Step 6 tests whether the prose follows the data, not just the numbers.

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

This is a fixed chain with one retry loop, not an autonomous agent. That is deliberate: Oshin
Ch. 8 frames LLM app design as a trade-off between agency and reliability, where chains trade
agency for reliability. The README should say so plainly.

### State

```python
class AnalysisState(TypedDict, total=False):
    ticker: str
    question: str
    metric_ids: list[str]          # plan
    facts: list[Fact]              # fetch
    metrics: list[MetricResult]    # compute
    drafts: list[str]              # write: every attempt, latest last (placeholders, not numbers)
    problems: list[str]            # check: problems in the latest draft (empty = passed)
    memo: str                      # render
    cost_usd: float                # running total; the budget guard reads it
```

`Fact` = line item, fiscal year, value, XBRL concept tag, accession number.
`MetricResult` = metric id, fiscal year, value (or `None`), reason if `None`, inputs used.

### Run record

Every run saves `runs/<ticker>-<timestamp>.json` with the question, chosen metrics, every draft,
the problems found, the number of attempts and the cost. This happens whether or not a memo was
produced. Huyen Ch. 6: "Always print out each tool call and its output so that you can inspect
and evaluate them." The memo goes next to it as `.md`.

## 4. Files

```
financial-analyst-agent/
├── fin_analyst/
│   ├── __main__.py   # CLI
│   ├── config.py     # config.yaml + .env; per-node model/effort/token cap + cost maths
│   ├── edgar.py      # Step 1: 10-K → facts            (no LLM)
│   ├── metrics.py    # Step 2: ratio functions          (no LLM)
│   ├── memo.py       # Step 3: placeholders + checks    (no LLM)
│   ├── llm.py        # Step 5: the only file that imports anthropic
│   └── graph.py      # Step 4: LangGraph nodes + wiring + run record
├── scripts/
│   └── record_fixture.py   # re-record a company's facts on purpose
├── tests/
│   └── fixtures/     # recorded 10-K facts, so tests need no network
├── config.yaml       # model, budget, retries, prices
└── runs/             # memos + run records (gitignored)
```

`tests/test_llm_isolation.py` fails if any file other than `llm.py`, `graph.py` or `__main__.py`
imports `anthropic` or `fin_analyst.llm`.

## 5. Steps

### Step 0 — Scaffold ✅
Venv, pinned requirements, `config.yaml`, CLI skeleton, isolation test.
**Done when:** `pytest` passes and `python -m fin_analyst --help` works.

### Step 1 — Fetch facts from one 10-K (`edgar.py`) ✅
- Ticker → latest 10-K → balance sheet, income statement and cash flow → `list[Fact]`, using
  edgartools (`set_identity`, `Company`, `get_filings(form="10-K")`, `filing.xbrl()`,
  `xbrl.statements...`). Check the exact calls against current edgartools docs.
- **Line items (only the ones a metric or check uses):**

  | Statement | Line items |
  |---|---|
  | Income statement (3 years) | revenue, gross profit, net income attributable to the parent |
  | Cash flow (3 years) | operating cash flow |
  | Balance sheet (2 year-ends) | total assets, total liabilities and equity, current assets, current liabilities, cash and equivalents, short-term investments, accounts receivable, equity attributable to the parent, short-term borrowings (incl. commercial paper), current portion of long-term debt, long-term debt (non-current) |

- **Tags:** XBRL tag names vary by company, so keep a small dict of candidate tags per line item
  in `edgar.py`. Take the first match and record which tag was used.
- **Parent vs total:** use the *parent-only* versions of net income and equity. A ratio's
  numerator and denominator must belong to the same owners (B&D Ch. 2, "Mismatched Ratios").
  XBRL has both `StockholdersEquity` (parent only) and
  `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`.
- **Lines not on the balance sheet:** short-term investments, receivables and the debt lines
  aren't presented by every company. If one isn't found, record it as `0` with the note
  "not reported, treated as 0", and list it in the memo footer. Every other missing line item is
  simply left out, so metrics that need it return `None`.
- Save MSFT's facts as a JSON fixture: `python scripts/record_fixture.py MSFT`.

**As built (MSFT FY2026 10-K, accession 0001193125-26-323660):** 35 facts, every value matching
the filing's own rendered balance sheet. Two rules earned their own tests:
- **Dimensioned rows must be dropped.** A filing tags the same concept again per segment and per
  equity component; those rows sit beside the whole-company total.
- **Only annual durations.** A 10-K also carries quarterly figures.

Equity comes with 3 year-ends (the statement of equity carries an extra year) while the balance
sheet has 2, so ROE covers 3 years but the equity multiplier covers 2.

**Done when:**
- total assets = total liabilities and equity for both year-ends (this catches wrong periods or
  scale; total liabilities alone isn't always tagged)
- current assets ≤ total assets
- every fact has an accession number

### Step 2 — Metrics (`metrics.py`) ✅
Nine metrics, using **ending** balances. This is simpler, B&D eq. 2.20 does the same, and its
footnote allows averages as the alternative. State it in the memo footer.

| id | formula | source |
|---|---|---|
| `current_ratio` | current assets / current liabilities | B&D §2.6 |
| `quick_ratio` | (cash + short-term investments + receivables) / current liabilities | B&D §2.6; Subramanyam Ch. 10 |
| `cash_flow_ratio` | operating cash flow / current liabilities | Subramanyam Ch. 10 |
| `debt_to_equity` | (short-term borrowings + current long-term debt + long-term debt) / parent equity | B&D eq. 2.15 |
| `gross_margin` | gross profit / revenue | B&D §2.6 |
| `net_margin` | net income / revenue | B&D §2.6 |
| `asset_turnover` | revenue / total assets | B&D eq. 2.23 |
| `equity_multiplier` | total assets / parent equity | B&D eq. 2.23 |
| `roe` | net income / parent equity (= net margin × asset turnover × equity multiplier) | B&D eq. 2.20, 2.23 |

Definition notes:
- **Quick ratio** counts only cash and "near cash" assets. It is *not* current assets minus
  inventory, which would also count prepaid and other current assets and overstate liquidity.
- **Cash flow ratio** is there because the current ratio is "static". A company can be
  "highly liquid even if these ratios are poor" (B&D §2.6), so this ratio uses a cash flow
  instead (Subramanyam Ch. 10).
- **Debt** excludes lease liabilities, following B&D. Subramanyam would include them; that's in
  Iteration 2. Say this in the memo footer. **Never** substitute total liabilities for debt.
- **Years:** the balance sheet has only 2 year-ends, so any metric with a balance-sheet input has
  2 years. Gross and net margin have 3.

Each metric is a small function plus a one-line description. The description is what `plan` shows
Claude.

**As built:** a year is only reported when the metric's denominator exists in the filing, so no
2024 current ratio is invented; ROE still covers 3 years because equity does. A company with no
debt lines at all gives "no debt lines", which is different from reported zeros.

**Done when:** tests pass for:
- values checked by hand against the MSFT 10-K
- DuPont product equals ROE
- a missing input gives `None` with a reason
- a zero denominator gives `None` with a reason
- zero or negative parent equity gives `None` for `roe`, `equity_multiplier` and
  `debt_to_equity`, with the reason "equity is not positive". Heavy buybacks can do this, and the
  ratios would mislead.

### Step 3 — Placeholders and checks (`memo.py`)
- `find_problems(draft, metrics, years) -> list[str]` covers leaked digits and unknown metric ids
  or years.
- `render(draft, metrics) -> str` formats values: ratios as `1.35x`, margins as `36.1%`, changes
  with "rose"/"fell"/"was unchanged". It adds a footer listing:
  - the filing's accession number and period end
  - "ending balances; debt excludes leases"
  - any line items treated as 0

**Done when:** tests cover a clean draft, a leaked number, a leaked percentage, an unknown
placeholder, an allowed year, the rendered direction words, a `None` metric, and the footer.

### Step 4 — The graph, with a fake Claude (`graph.py`)
- Wire the six nodes. The Claude-using nodes get their model client passed in, so tests can pass
  a fake one that returns scripted replies.
- Save the run record (§3) at the end of every run.
- **Done when:** tests cover:
  - happy path → memo + run record
  - leaky draft → retry → clean draft → memo, with both drafts in the record
  - three leaky drafts → gives up, no memo, record still saved
  - `plan` choosing an unknown metric → error

### Step 5 — Real Claude (`llm.py`)
- `plan`: one call with a strict tool whose `metric_ids` is an enum of the metric list.
- `write`: one call returning the draft text. The system prompt covers the placeholder rule, the
  available placeholders, and the word rules in §2. On retry, the prompt includes the problems
  found.
- Model `claude-opus-5`, adaptive thinking, and refusal fallbacks. Check `stop_reason` before
  reading.
- Each node reads its model, `effort` and `max_tokens` from `config.yaml`. Output tokens cost 5x
  input and drive latency, so the ceilings are the main cost lever (Huyen Ch. 4); the 250-word
  rule is the other half of it.
- Budget guard: after each call, add the cost from `usage` using that model's prices in
  `config.yaml`, and stop if it passes `max_usd_per_run` ($1.00). That cap is a runaway guard,
  not a budget: a memo is 2 calls and should cost a few cents.
- Prompt caching is deliberately **not** used yet. It pays off with long system prompts across
  many calls (Huyen Ch. 9); these prompts are short and may fall below the minimum cacheable
  size. Revisit when filing text enters the prompt in Iteration 2.
- **Before the first live run:** ask Willie, stating the estimated cost.

**Done when:** one live MSFT memo is saved in `runs/`, and the real cost is written in the README.

### Step 6 — Prove it
Ask Willie before these live runs, with the estimated cost.

- **Trend-flip test (prose follows the data):** edit the MSFT fixture so a real trend reverses.
  For example, lower the latest year's net income so net margin *falls* when the real 10-K shows
  it rising. Run the workflow and read the memo:
  - Pass: no words contradict the edited data ("improved", "expanded", "strengthened"), and there
    are no business reasons the data doesn't contain.
  - Record the result, either way.

  Editing numbers alone would pass trivially, because placeholders already guarantee them.
- **Plan check:** write 8–10 questions, each with the metrics that should answer it (e.g.
  "How liquid is it?" → current, quick, cash flow ratio). Run only the `plan` node on each and
  report how many plans were valid. This catches "goal failure", a valid plan that doesn't answer
  the question, which the unknown-metric test doesn't (Huyen Ch. 6).
- **Second company:** run one to see what breaks in the tag mapping, and fix or document it.
- **README:**
  - how it works: a reliability-first chain, not an autonomous agent (§3)
  - one example memo
  - cost per memo and average attempts
  - both test results
  - limitations: no peers, ending balances, leases excluded, banks

**Done when:** all of that is in the README.

## 6. Iteration 2 (later, only after Step 6)

Pick from these based on what Iteration 1 taught:
- Compare two companies, and handle different fiscal year-ends. Peers are what make ratios
  meaningful (Subramanyam Ch. 1).
- Multiple years across several filings, with restatement handling
- Include lease liabilities in debt (Subramanyam Ch. 1), as a labelled variant
- Cite MD&A and risk-factor passages (retrieval, reusing the textbook-kb stack), plus a
  claim-support check. That check would be an AI judge, and judges make mistakes too, so grade a
  sample by hand (Huyen Ch. 4).
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
  - **It is untrusted input.** News text can carry instructions aimed at the model ("indirect
    prompt injection", Huyen Ch. 5). Pass it inside clearly marked data blocks, tell the writer
    to treat it as quotes rather than instructions, and keep the writer without tools.
  - It supplies words only, never numbers; the existing digit check already enforces this.
  - Every claim carries a source link and publication date.
  - The memo shows news in its own section, labelled with its dates, so it isn't mixed up with the
    10-K's fiscal period.
- A per-run trace file and more evals (a hand-checked gold set, claim grading)
- Averages instead of ending balances; interest coverage and other solvency metrics
- Banks and insurers (current ratio doesn't apply)
- Publish: public repo, blog post

## 7. Known traps

- **XBRL values are in raw units, not millions.** Format at render time only.
- **Fiscal years differ** (MSFT's ends in June). Label memos with the period-end date.
- **SEC needs an identity:** set `SEC_USER_AGENT`, or requests get 403s.
- **Parent vs total equity:** mixing them breaks ROE and the DuPont identity.
- **Dimensioned XBRL rows** repeat a concept per segment or equity component. Taking one by
  mistake silently gives a wrong number that still looks plausible.
- **Total liabilities isn't always tagged.** The balance check uses total liabilities and equity
  instead.
- **Debt has no single tag.** Add up its parts; never fall back to total liabilities.
- **Negative equity** at buyback-heavy companies makes ROE and leverage ratios meaningless, so
  they return `None`.
- **Banks** have no current assets or liabilities, so those metrics return `None`, which is correct.
