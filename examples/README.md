# An example run, start to finish

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
