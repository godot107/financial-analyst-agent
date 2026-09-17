# Two example runs, start to finish

## 1. A memo that took two drafts

Both files come from one real run:

```bash
python -m fin_analyst MSFT "Why did Microsoft's gross margin percentage change, and what does the filing say about it?"
```

- **`msft-gross-margin-memo.md`** — what was published: every figure rendered from the filing, every
  explanation carrying a citation, each cited passage quoted underneath, and a footer naming the
  accession number.
- **`msft-gross-margin-run.json`** — the audit trail: the metrics chosen, **both** drafts (including
  the rejected one), what was wrong with it, all eight claim verdicts, and the cost.

It took two drafts, which is what makes it worth publishing. The claim check rejected the first
memo's opening line:

> **Claim:** "Gross margin percentage declined *again* this year, and the filing attributes the
> pressure to AI infrastructure investment..."
>
> **Verdict:** not supported — "neither passage says it declined 'again this year' (no prior-year
> decline mentioned)".

Margins had in fact declined the prior year, and the metrics show it. The cited passages don't say
so, and the check enforces support by the citation rather than truth in general. The rewrite dropped
the word.

Cost: $0.0863, higher than a typical memo because of the rejected draft.

The filing text quoted here is from Microsoft's public 10-K (accession 0001193125-26-323660), as
published on SEC EDGAR.

## 2. A company the usual ratios don't fit

```bash
python -m fin_analyst JPM "How liquid is JPMorgan?"
```

- **`jpm-liquidity-memo.md`** — what was published: all three liquidity ratios reported as
  unavailable, with the structural reason, and what the filing does say about liquidity governance
  instead, each claim cited and checked.
- **`jpm-liquidity-run.json`** — the same run as data, and this one carries the newer fields: the
  `filing` it used (company, CIK, form, filing date, period), each metric with the figures behind
  it, the passages, three claim verdicts, and the full `trace` of 15 events with each Claude call's
  tokens and cost.

The refusal is the point. Computing those ratios from a bank's tags gave 0.18x debt to equity
before this was fixed — wrong, and plausible enough to publish. The bank's own ratios (efficiency,
loans to deposits, credit cost) are computed instead when the question asks for them.

Cost: $0.0491, one draft, from the gold set run on 2026-09-17.

*Footnote: this memo's footer predates a wording fix. It lists lines "treated as zero" where a
bank simply has none of them; the footer now says so.*
