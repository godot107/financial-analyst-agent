# Agentic Financial Analyst

Ask a question about a public company, get a short memo built from its latest 10-K, where **every
number comes from the filing and none is written by the model**.

[Quick start](QUICKSTART.md) · [Example run](examples/) · [Write-up](BLOG.md)

```bash
python -m fin_analyst MSFT "How liquid is Microsoft, and what drives its return on equity?"
```

Claude chooses which ratios answer the question and writes the prose. Python pulls the filing,
computes every ratio, and fills in every figure. A checker rejects any draft where the model typed
a number itself, and a second Claude call verifies that each cited explanation is actually in the
passage it cites.

228 tests, none of which touch the network or need an API key. A memo costs a few cents.

## How it works

```
START → plan → fetch → compute → write → check ──ok──→ render → END
                                   ▲        │
                                   └─retry──┘ (up to 2, then it gives up)
```

Claude does two things: picks which ratios answer the question, and writes the prose. Python does
everything else — pulls the filing, computes the ratios, and fills in every figure.

**Claude cannot type a number.** It writes placeholders (`{{roe:2026}}`, `{{net_margin:2025->2026}}`)
and a checker rejects any draft containing a digit outside one. The renderer supplies the value
*and* the direction, so "fell 0.12x to 1.23x" is generated from the data, not asserted by the model.
If three drafts fail, the run publishes nothing and says why.

This is a deliberately fixed workflow, not an autonomous agent: it trades freedom for reliability.

## Results

| Check | Result | Cost |
|---|---|---|
| First live memo (MSFT, 7 metrics) | 334 words, 1 draft, no leaks | $0.0322 |
| **Trend-flip test** | **passed** | $0.0289 |
| **Plan check**, 10 questions | **10/10 chose metrics that answer the question** | $0.0695 |
| Declines what it can't answer (P/E, peer comparison) | passed | $0.0263 |
| Two-turn conversation | second answer built on the first | $0.0574 |
| Peer comparison (MSFT vs GOOGL) | both filings cited, year-end mismatch stated | $0.0624 |
| MD&A citations ("why did gross margin change?") | 4 passages cited and quoted | $0.0560 |
| Batch of two (MSFT, COST) | both answered first try | $0.0615 |
| Memo with citations + claim check | 1 draft, 3 claims checked, all supported | $0.0429 |
| **Claim-check grading**, 8 hand-labelled cases | **8/8 agreed** | $0.0167 |
| **Gold set, figures**: 5 companies incl. a bank and an insurer, checked against the 10-Ks' text | **77/77** | $0 |
| **Gold set, questions**: 10 through the whole workflow | **33/36**; 9/10 memos on the first draft | $0.5199 |

**The trend-flip test is the one that matters.** Claude has seen Microsoft's real financials in
training, so a memo that reads well may be memory rather than retrieval. Placeholders already
guarantee the numbers, so the test targets the *words*: net income in the recorded filing is cut
until net margin falls instead of rises, and the memo is then read for any claim that profitability
improved. It wrote "profitability fell in fiscal 2026", and every directional sentence agreed with
the altered data.

```bash
python evals/gold.py --free   # $0: figures and ratios vs the filings' own text
python evals/gold.py --spend  # ~$0.60: 10 questions through the whole workflow
python evals/trend_flip.py    # ~$0.03
python evals/plan_check.py    # ~$0.07
python evals/claim_check.py   # ~$0.02
```

**The gold set** (`evals/gold.yaml`) holds figures found by hand in the text of four 10-Ks
(Microsoft, Costco, Apple, JPMorgan, Travelers), each with the line it came from, and twelve
questions. The free
level checks extraction and every ratio formula against those figures, plus what must be refused
(Costco has no gross profit line, Apple no interest expense, JPMorgan no current/non-current
split). The paid level runs the questions and grades the plan, the memo, whether the first draft
passed, and wording the memo must or must not contain. Its first run scored 33/36 for $0.52:
nine memos passed on the first draft, and the tenth (Costco leverage over three filings) gave up
after three. Two drafts were lost to the digit check flagging "Item 7" and "53-week", both quoted
from the filing. The third was rightly rejected: it cited a passage to say what the filing "notes
only", and the passage said more. The Apple check failed on the grader, not the memo, which said
"cannot be computed" where the grader wanted "not available". All four are fixed, and re-running
the Costco and Apple questions scored 7/7 and 7/7 ($0.17), every memo on its first draft.

**The claim checker is itself graded**, because AI judges err too (Huyen Ch. 4). Eight claims are
paired with real passages and labelled by hand, and the hard cases are not opposites but claims
that are plausible, probably true, and simply absent from the passage. The judge got all eight,
including "AI talent was the single largest driver", which the passage never ranks.

## What the checks caught

Running a second and third company found two real defects, both now fixed and covered by tests:

- **Fiscal years were wrong for late-August filers.** Costco's FY2024 figures arrive from
  edgartools labelled 2025, so last year's balance sheet silently overwrote this year's and the
  ratios came out of mixed data. The fiscal year is now taken from the period end date.
- **A missing tag became a zero.** Costco tags receivables differently, and treating that as zero
  understated its quick ratio while still looking like an answer. The quick ratio now needs a
  reported receivables or short-term-investments line, or it reports itself unavailable.

A cosmetic one too: the writer sometimes typed "fell" in front of a placeholder that renders its
own verb, producing "fell fell 0.12x". The renderer now drops the writer's word.

The claim-check eval then exposed a **retrieval** bug rather than a judging one. Asked why operating
expenses rose, BM25 ranked a paragraph about currency effects above the paragraph that answers it:
the currency text repeats "expenses", while the question's "increase" never matched the filing's
"increased". Light stemming and credit for matching phrases fixed the ranking. A test of that fix
then found a second bug: with few passages BM25 gives common terms negative weight, so filtering on
a positive score silently returned nothing at all.

Adding news then exposed a contradiction of my own making: the prompt told the writer to date its
news, while the checker rejected every digit — so the first news run failed closed after three
drafts, on a date. Dates the sources themselves carry now pass; everything else is still caught.
A related rule became checkable rather than advisory: a change placeholder renders a verb phrase
("fell 0.12x to 1.23x"), so one opening a clause, with no subject, is now a rejected draft.

Two false positives were costing real money, each burning a $0.03 retry: **"FY2026"** failed the
leak check (there is no word boundary between "Y" and "2"), and **"Microsoft 365"** failed it too,
because a product name with digits looked like a figure. Names the filing itself uses are now
allowed; measurements never are, since they carry a decimal, a currency symbol or a percent sign.

## Saying *why*

The memo can explain a movement, but only from the filing's own words:

```bash
python -m fin_analyst MSFT "Why did Microsoft's gross margin percentage change?"
```

> Gross margin percentage decreased, driven by continued investments in AI infrastructure and a
> sales mix shift to Azure, offset in part by efficiency gains [P30].

Every explanation is then **checked by a second Claude call**: is this claim actually in the passage
it cites? An unsupported claim goes back to the writer as a problem, like a leaked number, and a
memo whose claims never hold up publishes nothing. Every verdict — not only the failures — is saved
in the run record. `--no-verify` skips it.

Item 7 and Item 1A are split into paragraphs and searched with **BM25** — exact terms, no vector
store, no embedding model. Financial questions hinge on exact words ("operating expenses",
"ASC 842") that embeddings blur, and Huyen Ch. 6 notes term-based retrieval "works well out of the
box". Every explanation carries a citation, each cited passage is quoted under the memo, and
**numbers never come from the narrative** — those still come only from placeholders. Passage text
is handed to the model as quoted material, never as instructions.

`--no-text` skips it when you only want the ratios.

## What happened since the filing

A 10-K can be a year old. `--news` adds the company's recent 8-K press releases, cited as `[N1]`
and held to the same rules: words only, every claim carries its source and its date, and nothing
from them is presented as part of the 10-K.

```bash
python -m fin_analyst MSFT "What has it said recently about AI capacity?" --news
```

`--news-index` adds GDELT headlines too, but it is off by default for a measured reason: even
restricted to business desks, the index answered a question about Microsoft with market commentary
("Cramer reveals his favorite Mag 7 stock") rather than company facts. The company's own filings
are the better source.

## Comparing two companies

```bash
python -m fin_analyst MSFT "How does its liquidity compare with Alphabet's?" --peer GOOGL
```

The peer's ratios arrive as `{{peer.roe:2025}}` placeholders, computed exactly the same way. Because
fiscal years rarely line up, each company is shown at **its own latest year end** and the memo says
so — Microsoft's year ends in June, Alphabet's in December. Both filings are cited in the footer.

## Conversation

```bash
python -m fin_analyst MSFT "How liquid is Microsoft?" --chat
```

Follow-ups reuse the filing (the SEC is hit once) and see the earlier answers. Each turn resends
those answers, so every turn costs a little more than the last — hence a hard ceiling of **5 turns**
and a single budget shared across the whole conversation, not per turn.

## As a service

```bash
python -m fin_analyst.server      # needs FIN_ANALYST_API_KEYS; see QUICKSTART
```

A memo takes 30–90 seconds, longer than an API gateway will hold a request open, so the service
takes a job and answers at once: `POST /v1/memos` returns `202` and an id, `GET /v1/memos/{id}`
returns the memo when it's ready. Every route but health needs a key, and daily spending caps are
checked when a job is submitted, before any Claude call. See [`docs/API_PLAN.md`](docs/API_PLAN.md)
for the design and what deploying it involves, and [`docs/CALLING.md`](docs/CALLING.md) for calling
the deployed Lambda (SigV4-signed HTTPS) and reading a run's trace.

Every run keeps a **trace**: each step, its timing and output, and each Claude call's tokens, cost
and a summary of its thinking. `--verbose` prints it live; the run record and the API's job result
keep it.

## Two helpers for repetitive work

```bash
python scripts/coverage.py MSFT COST JPM        # free: no model, no API key
python scripts/batch.py "How liquid is it?" MSFT COST JPM --dry-run
python scripts/batch.py "How liquid is it?" MSFT COST --max-usd 0.30
```

**`coverage.py`** shows which line items a filing gave up, which XBRL tag matched each one, what was
treated as zero, and which ratios that costs. Both real defects so far were tag problems that
surfaced only as a memo reading slightly wrong; this shows them in seconds.

**`batch.py`** asks one question across a watchlist, writing a memo per company plus a summary
table into a dated folder. A company that fails is recorded and the batch carries on, and a total
spend cap sits above the per-memo guard. Two companies came to $0.0615.

## Install

See [QUICKSTART.md](QUICKSTART.md) for the five-minute version, including the two things that trip
people up (run from the project root; use the virtualenv).

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # SEC_USER_AGENT="Your Name you@example.com", ANTHROPIC_API_KEY=...
pytest
python -m fin_analyst MSFT "How liquid is it?" --dry-run   # spends nothing
```

**Run it from the project root**, the folder holding `fin_analyst/`, not from inside the package —
`python -m fin_analyst` looks for the package in the current directory.

**Use the virtualenv.** Either activate it (`source .venv/bin/activate`, after which `python` is the
venv's Python and your prompt shows `(.venv)`), or call it directly without activating:

```bash
.venv/bin/python -m fin_analyst MSFT "How liquid is it?" --dry-run
```

On Ubuntu the system command is `python3`, not `python`; inside the virtualenv both work. Running
with system Python fails on imports, because the dependencies live in `.venv`.

Cost per memo is about **$0.03**, against a $1 per-run guard that aborts a runaway. Model, effort
and token ceilings are set per node in `config.yaml`.

## Limitations

- **Up to five filings per company** (`--filings N`). Where a later 10-K restates a year, the later
  figure is used and the footer names what changed. A change in which XBRL tag matched looks the
  same as a restatement, so the footer says "restated or tagged differently". Peer comparison is
  one company against one other, at year ends that usually differ.
- **Market data is one price.** With `--market` and a free Alpha Vantage key you get P/E,
  market-to-book and EV/revenue, computed from the filing's diluted share count and a current
  price. That mixes a price from today with a fiscal year's figures, which the footer says
  explicitly. Alpha Vantage's *fundamentals* are deliberately unused: no accession number.
- **Banks and insurers** (no current/non-current split) get their own ratios: a bank's efficiency
  ratio, loans to deposits, credit cost to loans and net interest income over average assets; an
  insurer's claims and underwriting costs against premiums earned. Liquidity, debt and interest
  coverage ratios say they don't apply: the debt tags this tool reads found a sliver of JPMorgan's
  borrowing (0.18x debt to equity) and next to none of Travelers' (0.00x).
- **Two bank and insurer measures are deliberately missing.** Regulatory capital (Tier 1, CET1) is
  tagged per legal entity, which this tool drops along with every other segment breakdown. And an
  insurer's headline **combined ratio** is a statutory measure: Travelers reports 89.9% where the
  income statement gives 92.5%, because its ratios adjust for fee income and use written rather
  than earned premiums. Rather than publish a number 2-3 points off the one in the filing, the
  ratios here are named for what they are (`claims_to_premiums`, `underwriting_cost_to_premiums`).
- **Interest coverage needs an interest expense line.** Apple stopped reporting one after FY2023, so
  its latest years show as unavailable rather than ending quietly at 2023.
- **Retailers that don't tag gross profit** (Costco) get no gross margin rather than a derived one.
- **Ending balances, not averages,** except `roe_average_equity`. **Debt excludes lease
  liabilities,** except `debt_to_equity_with_leases`. The footer says which applied. Lease
  liabilities need either a total or both current and noncurrent parts tagged; half is treated as
  unreported.
- **52/53-week years ending in early January** are dated to the following calendar year. The footer
  always shows the period end date, so it is visible rather than hidden.
- Not investment advice: no price targets, no buy/sell calls.

## How it was checked

Each layer of checking found a defect in the layer beneath it, and every one is now covered by a
test:

- **Running a second company** found fiscal years taken from an unreliable column: Costco's FY2024
  figures arrive labelled 2025, so last year's balance sheet overwrote this year's. Microsoft's June
  year-end never collides, so one company would never have shown it.
- **The same run** found a missing XBRL tag being read as zero, producing a quick ratio that was
  wrong and looked reasonable.
- **Grading the claim checker** found a *retrieval* bug: the search ranked a paragraph about currency
  effects above the one explaining operating expenses. The judge had been right about the wrong
  paragraph.
- **Testing that fix** found that with few passages, BM25 gives common terms negative weight, so
  filtering on a positive score returned nothing at all.
- **Reading the first trace from Lambda** found four things every check had passed. The company's
  name in the question pulled three Microsoft 365 revenue paragraphs into a liquidity memo, and
  "liquid" didn't match "liquidity". The writer cited two of those paragraphs only to dismiss them,
  and the claim checker accepted that. It repeated values the prompt had shown as a bad example.
  It also judged a ratio in words ("no longer covers near-term obligations") where the digit check
  can't see a rule of thumb. The search now leaves out the company name, the checker catches
  repeats and rule-of-thumb wording, and the claim checker rejects citations used only to dismiss.
  Re-checking the twelve earlier memos that passed found the same problems in three of them.

## Reading further

- [`QUICKSTART.md`](QUICKSTART.md) — setup, every command, what each costs, and what the errors mean
- [`examples/`](examples/) — a real memo and its full run record, including a rejected draft
- [`BLOG.md`](BLOG.md) — why it is built this way, and what the checks caught
- [`BLOG-2.md`](BLOG-2.md) — deploying it, reading its first trace, and what that trace caught
- [`PLAN.md`](PLAN.md) — the build spec, step by step, and what is still unbuilt
- [`CLAUDE.md`](CLAUDE.md) — the invariants, for anyone (or any agent) changing the code

Definitions follow Berk & DeMarzo, *Corporate Finance* Ch. 2 and Subramanyam, *Financial Statement
Analysis*; the evaluation design follows Huyen, *AI Engineering*. Filing text in the test fixtures
comes from public SEC EDGAR filings.

## Authorship

Built with [Claude Code](https://claude.com/claude-code). The design decisions, the review and the
evaluation are mine; most of the code was model-written. MIT licensed.
