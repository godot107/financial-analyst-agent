# I built an AI financial analyst that isn't allowed to write numbers

Ask a language model to analyse a company's accounts and you get something that reads beautifully
and cannot be trusted. Not because the model is careless — because you cannot tell, from the page,
which figures came from the filing and which came from the model's memory of a company it has read
about ten thousand times.

So I built one where that question has a structural answer instead of a hopeful one.
[`financial-analyst-agent`](https://github.com/godot107/financial-analyst-agent) answers a question
about a public company from its latest 10-K. Claude chooses which ratios to use and writes the
prose. **Python computes every number, and Claude is not permitted to type one.**

## The rule, and how it's enforced

The model writes placeholders:

```
Its current ratio {{current_ratio:2025->2026}}, and the quick ratio {{quick_ratio:2025->2026}}.
```

A renderer fills them in from the filing:

> Its current ratio fell 0.12x to 1.23x, and the quick ratio fell 0.23x to 0.93x.

Then a checker rejects any draft containing a digit outside a placeholder. If three drafts in a row
fail, the run publishes nothing and records why. Failing closed matters: a memo with one invented
figure is worse than no memo, because it spends credibility it hasn't earned.

Note what the renderer supplies: not just the value, but the direction. "Fell" is generated from the
data. The model never gets to decide whether a number went up.

## The test that mattered

Here is the trap in evaluating something like this. Claude has seen Microsoft's financials many
times in training. A memo about Microsoft can be entirely accurate and entirely *recalled* — the
model writing what it remembers, and the filing agreeing by coincidence.

Placeholders already guarantee the numbers, so the residual risk lives in the **words**: "margins
expanded", "profitability improved". That's what I tested. I took the recorded filing, cut net
income until net margin *falls* instead of rises, and ran the workflow against the altered data.

The memo opened: *"profitability fell in fiscal 2026."* Every directional sentence matched the
altered data. Retrieval, not recall.

## Then I let it explain itself, and checked that too

Ratios say what moved; only the filing's narrative says why. So Item 7 and Item 1A get split into
paragraphs and searched with BM25 — plain term matching, no vector database. Financial questions
turn on exact words ("operating expenses", "ASC 842") that embeddings blur.

Every explanation must carry a citation, and each cited passage is quoted under the memo. Numbers
still never come from the narrative: the model may cite a paragraph saying expenses rose $4.9
billion, and may not repeat the figure.

Then a second Claude call asks, for every cited sentence: **is this claim actually in the passage
it cites?** On a real run it rejected the memo's own opening line:

> **Claim:** "Gross margin percentage declined *again* this year, and the filing attributes the
> pressure to AI infrastructure investment..."
>
> **Judge:** "Passages state gross margin percentage decreased, but neither says it declined 'again
> this year' — the 'declined again' framing is unsupported."

Margins *had* declined the prior year; the metrics show it. But the cited passage doesn't say so,
and the judge enforces support by the citation, not truth in general. That's the stricter and more
useful standard, and the memo was rewritten before anyone read it.

Because AI judges make mistakes too, the judge is itself graded: eight claims paired with real
passages, labelled by hand, with the hard cases being claims that are plausible and probably true
but simply absent. It got eight out of eight.

## Every layer of checking found a bug in the layer beneath

This is the part I didn't expect, and the part I'd tell anyone building on filings.

**Running a second company found a wrong fiscal year.** Everything was right for Microsoft. Costco's
year ends in late August, and its FY2024 figures arrive from the parsing library *labelled 2025* —
so last year's balance sheet quietly overwrote this year's, and the ratios came out of mixed data.
Microsoft's June year-end never collides, so one company would never have shown it.

**A missing tag became a zero.** Costco tags receivables differently from Microsoft. The code read
the absence as zero, which produced a quick ratio that was wrong and looked perfectly reasonable —
exactly the failure the whole project exists to prevent. "Absent means zero" is only safe when at
least one line in the group is actually reported.

**Grading the judge found a retrieval bug.** My first grading run scored 6/8 and looked like judging
errors. It wasn't. Asked why operating expenses rose, the search ranked a paragraph about *currency
effects* above the paragraph that answers the question: the currency text repeats "expenses", while
the question's "increase" never matched the filing's "increased". The judge had been right both
times — about the wrong paragraph.

**And the test for that fix found one more.** With only a handful of passages, BM25 gives common
terms negative weight, so filtering on "score above zero" silently returned nothing at all.

Four defects, none found by unit tests written against the happy path. Each was found by a check
aimed one level up.

## What it costs

A memo is two Claude calls, plus one more when it cites the filing:

| | |
|---|---|
| Memo, ratios only | ~$0.03 |
| Memo with citations and the claim check | $0.04–0.09 |
| Plan check across 10 questions | $0.07 |
| Grading the judge | $0.02 |

The whole project — every live run, every eval, every retry — cost under a dollar. There's a
per-run cap that aborts a runaway, but it's a guard, not a budget.

Two cost lessons. First, false positives in the checker cost real money: "FY2026" failed the leak
check (no word boundary between "Y" and "2") and so did "Microsoft 365", because a product name
with digits looks like a figure. Each burned a $0.03 retry until fixed. Second, prompt caching
isn't free money — it pays off with long prompts across many calls, and these prompts are short, so
I left it out and wrote down why.

## Choices I'd defend

**LangGraph, but a fixed graph.** Six nodes, one retry loop, no autonomy. Nothing here benefits from
the model deciding what to do next; the value is in what it's prevented from doing.

**Pre-written ratio functions, not generated code.** The model picks metric ids from a list. Nine
ratios, each with a textbook definition attached — which it turned out to need: given only a name
and a value, it described the quick ratio as "current assets minus inventory", which isn't the
formula used here.

**Filings only.** Market-data APIs give tidy normalized fundamentals with no accession number
attached. That breaks the audit trail, which is the entire point.

## What it can't do

No peer benchmarks beyond one company at a time, no market data (so no P/E), and bank leverage
ratios that understate borrowing because the tags miss most of it. Ratios use ending balances;
debt excludes leases. Every memo says which of these apply, in a footer with the filing's accession
number, because a limitation stated is a limitation a reader can price in.

It is not investment advice, and it produces no price targets.

---

*Built with Claude Code — the design decisions, review and evaluation are mine, most of the code is
model-written. 188 tests, none of which touch the network or an API key. Code:
[github.com/godot107/financial-analyst-agent](https://github.com/godot107/financial-analyst-agent)*

---

## Social drafts

**LinkedIn**

> I built an AI financial analyst that isn't allowed to write numbers.
>
> Ask a language model about a company's accounts and you get prose that reads well and can't be
> trusted — you can't tell which figures came from the filing and which came from memory of a
> company it has read about ten thousand times.
>
> So the model writes placeholders, `{{current_ratio:2025->2026}}`, and Python fills in the value
> *and the direction*. Any digit the model types itself fails a check. Three failed drafts and it
> publishes nothing.
>
> To prove it's retrieval rather than recall, I edited the filing — cut net income until margins
> fell instead of rose — and re-ran it. The memo said "profitability fell". It followed the data,
> not its memory.
>
> The part I didn't expect: every layer of checking found a bug in the layer beneath it. Running a
> second company exposed a fiscal-year bug that Microsoft alone would never have shown. Grading the
> AI judge exposed a retrieval bug, not a judging one — the judge had been right about the wrong
> paragraph.
>
> Whole thing cost under a dollar to build and evaluate. Write-up and code in the comments.
>
> #AI #LLM #FinancialAnalysis #Python

**X / Twitter**

> Built an AI financial analyst that can't write numbers.
>
> It emits `{{roe:2026}}`; Python fills in the value and the direction. Any digit it types itself
> fails the check.
>
> Proof it isn't recall: I edited the filing so margins fell instead of rose. The memo said they
> fell. 🧵

> Every check found a bug one layer down:
>
> • 2nd company → fiscal years silently wrong (Microsoft alone never showed it)
> • missing XBRL tag → read as zero → a plausible wrong ratio
> • grading the AI judge → the *retrieval* was wrong; the judge was right about the wrong paragraph
>
> Total spend: under $1.
