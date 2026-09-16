# Agentic Financial Analyst

Ask a question about a company, get a short memo built from its latest 10-K, where **every number
comes from the filing and none is written by the model**.

```bash
python -m fin_analyst MSFT "How liquid is Microsoft, and what drives its return on equity?"
```

> Iteration 1 complete; iteration 2 has peer comparison. 101 tests, none of which touch the network.

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

**The trend-flip test is the one that matters.** Claude has seen Microsoft's real financials in
training, so a memo that reads well may be memory rather than retrieval. Placeholders already
guarantee the numbers, so the test targets the *words*: net income in the recorded filing is cut
until net margin falls instead of rises, and the memo is then read for any claim that profitability
improved. It wrote "profitability fell in fiscal 2026", and every directional sentence agreed with
the altered data.

```bash
python evals/trend_flip.py    # ~$0.03
python evals/plan_check.py    # ~$0.07
```

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

The peer run then caught a false positive that was costing real money: "FY2026" failed the leak
check, because there is no word boundary between "Y" and "2", so a perfectly legal fiscal year read
as a typed number and burned a retry.

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

- **One filing per company.** Peer comparison is one company against one other, at year ends that
  usually differ; there is no common-period restatement.
- **No market data,** so no P/E, EV/EBITDA or market-to-book.
- **Banks:** liquidity ratios are correctly unavailable (no current assets), but debt to equity is
  computed from tags that miss most bank borrowing, so it understates leverage. Don't trust it.
- **Retailers that don't tag gross profit** (Costco) get no gross margin rather than a derived one.
- **Ending balances, not averages.** Debt excludes lease liabilities. Both are stated in the memo.
- **52/53-week years ending in early January** are dated to the following calendar year. The footer
  always shows the period end date, so it is visible rather than hidden.
- Not investment advice: no price targets, no buy/sell calls.

See [`PLAN.md`](PLAN.md) for the build spec and what iteration 2 would add.
