# I deployed the analyst, read its first trace, and found four mistakes every check had passed

[Part one](BLOG.md) was about an AI financial analyst that isn't allowed to write numbers: Claude
writes placeholders, Python fills in the figures, and a checker rejects any digit the model typed.
This part is what happened when it left my laptop. It went onto AWS Lambda, got a trace that shows
each step it takes, and was tested against figures I found by hand in four 10-Ks.

The short version: the checks I had were aimed at the failures I'd already imagined. The first trace
from production showed me four I hadn't.

## Putting it behind HTTPS

A memo takes anywhere from 20 seconds to over a minute. That rules out answering in the request
itself: API Gateway gives up at 30. So the service takes a job and answers straight away with an id. The Lambda function then
invokes itself asynchronously to write the memo, and the caller polls for it. Jobs and cached filings
live in S3, secrets in SSM Parameter Store, and nothing sensitive is baked into the image.

The URL has **two locks**, and a request needs both:

| Lock | Checked by | Without it |
|---|---|---|
| An AWS SigV4 signature | AWS, before my code runs | 403 |
| An `X-API-Key` header | the app | 401 |

The signature says *this caller may reach the function at all*. The key says *which caller it is*,
so a daily spending cap can be charged to them. The cap is checked when the job is submitted, before
any Claude call: an uncapped endpoint to a paid model is an open tab.

Signing was the part worth practising. SigV4 never sends the secret. It hashes a canonical form of
the request, body included, and signs that with a key derived from the secret, the date, the region
and the service. Change one byte in transit and the signatures stop matching. A signature is
accepted for five minutes, so a captured request is useless shortly after. curl can do it natively.
The one habit to build is passing credentials on stdin (`-K -`) rather than `--user`, where any
process on the machine can read them.

The first memo through it: **$0.0453**, one draft, both cited claims confirmed.

## Watching it think, which mostly meant watching it work

I wanted to see the agent's reasoning between steps, so every run now keeps a trace:
- each step, with its timing and output;
- every Claude call, with its tokens, cost and stop reason;
- every retry decision, such as `route check -> write`.

On Lambda each event is a JSON line in CloudWatch, so `aws logs tail --follow` shows a memo being
worked out as it runs.

I also asked for summaries of Claude's thinking. The first live run returned **zero thinking blocks
on all three calls**. Adaptive thinking decides for itself whether a call needs it, and at the
effort levels these calls run at, it decided they didn't. The writer's 619 output tokens were the
draft and nothing else. More effort would buy more thinking at a higher price per memo, and I
haven't tried it yet.

The trace was useful anyway, because the interesting part was never the model's inner monologue.
It was the draft, what was retrieved, and what the judge said.

## What the first trace showed

The memo passed every check, and reading the trace turned up four problems anyway.

**Retrieval matched the company's name.** The question was "How liquid is Microsoft?" After
stopwords, the search had two words to work with, "liquid" and "Microsoft". Only one paragraph
in the whole filing contained "liquid". Paragraphs about liquidity say *liquidity*, and the stemmer
didn't map one to the other. So "Microsoft" did the ranking, and three of the four passages the
writer got were about Microsoft 365 revenue.

**The writer cited passages only to dismiss them, and the judge accepted it.** *"Passages on
Microsoft 365 revenue growth [P25][P26] speak to earnings, not liquidity."* The claim checker asks
whether a claim is supported by the passage it cites. Its verdict: *"Both passages concern revenue,
not liquidity, as characterized."* That's a true statement, but it's about the passages, not about
the company. A citation is there to back a claim, not to write off a source.

**It repeated values the prompt had shown as a bad example.** *"The current ratio fell 0.12x to
1.23x, leaving it at 1.23x."* The prompt literally lists this pattern under "bad". Asking hadn't
been enough.

**It judged a ratio in words.** *"At 0.93x, the strictest measure no longer covers near-term
obligations."* That's the "above 1 is healthy" rule of thumb the rules forbid. It got through
because it has no digits, and the digit check was the only enforcement.

Then I ran the new checks over the twelve memos that had passed before. Three had the same
problems: one Costco memo said "above parity", and two Microsoft memos repeated values, one of them
also calling liquidity "adequate". So these weren't one-off slips. The old
checks were blind to them.

The fixes, in order of how much I trust them:
1. The search leaves out the company's own name. It uses the name from EDGAR, not the ticker,
   because tickers are often ordinary words: Costco's is COST.
2. "-ity" and "-ities" now stem, so "liquid" finds "liquidity" and "liabilities" finds "liability".
3. The checker rejects a change followed by its own end value in the same sentence, and rules of
   thumb written in words. Management's own "adequate" is still allowed in a sentence that cites it.
4. The writer and the judge are both told that a citation used only to dismiss a source is not
   support.

The first two are code, with tests pinned to the real filing. The third is a regular expression,
which is honest but blunt. The fourth is a prompt, which is a request.

One more thing from reading that trace. I'd noted the memo "ran long", about 330 words against a
250-word target. When I counted the words, the draft was 257; the rest was the cited sources and
the footer, which the code adds. Nothing needed fixing.

## Don't make anyone parse the Markdown

The deployed service is meant to feed other programs, and a program shouldn't parse prose. The
result now carries:
- the filing: company, CIK, form, filing date, and a sec.gov link that resolves;
- each ratio with its value, formatted value, and the filing figures it was computed from;
- each passage, marked cited or not.

The filing details come from a second EDGAR lookup. If a new 10-K lands between that lookup and the
one that fetched the numbers, the details would describe a filing the numbers didn't come from. So
they're checked against the facts' accession number, and dropped on a mismatch.

## More ratios, and the ones it now refuses

Part one ended with a list of what it couldn't do. Working through that list found as much as the
trace did.

**Leases.** Subramanyam treats lease liabilities as the financing they are, so there's now a
`debt_to_equity_with_leases` variant. Companies tag leases two ways: Microsoft reports only a total,
while Costco and Apple report current and noncurrent parts. Taking the first tag found would have
given Costco a lease figure for only one of its two balance sheets. With leases, Microsoft's debt to equity goes from 0.09x to 0.29x: it carries
$66.6 billion of finance leases, reported outside its debt lines.

**Interest coverage, and a line that stopped existing.** Apple stopped reporting interest expense
after fiscal 2023. Reading three filings, its interest coverage simply *ended in 2023*. Nothing
marked it as old, so a writer would present the 2023 figure as current. Each ratio now reports on
every year of its statement, and a missing line shows as "not available" for that year.

**Banks.** JPMorgan came out at **0.18x debt to equity**, and Travelers at **0.00x**. Bank balance
sheets aren't split into current and noncurrent, so the debt tags this tool reads found a sliver of
JPMorgan's borrowing and next to none of Travelers'. Those numbers were wrong and plausible, the worst
combination. Liquidity, debt and interest ratios now say they don't apply to a balance sheet like
that. Return on equity and the equity multiplier still work.

**Several filings,** with restatements. `--filings 3` reads three 10-Ks. When filings disagree about
a year, the later figure wins and the earlier one is kept and named in the footer.

That change nearly shipped a bug of its own. Filings are cached by accession number, because a filing
never changes. But what I extract from it *had* just changed, so every cached filing would have
kept answering without the new lines. The cache key now carries an extraction version.

## A gold set, checked against the filings' text

Unit tests prove the code does what I meant. They don't prove I meant the right thing. So the gold
set holds figures I found by hand in the *text* of four 10-Ks: Microsoft, Costco, Apple and
JPMorgan. Each figure quotes the line it came from. They were never copied from this tool's own
output.

The free level checks extraction and every ratio formula against those figures. It also checks what
must be refused: Costco has no gross profit line, Apple no interest expense, JPMorgan no current
assets. **57 of 57 pass.** I also fed it a wrong figure on purpose, to make sure it can fail. It
caught the wrong equity, the ratio built on it, and a refusal given for the wrong reason.

The paid level asks ten questions through the whole workflow and grades:
- whether the chosen ratios answer the question;
- whether a memo was published, and on the first draft;
- wording the memo must or must not contain, such as no "Microsoft 365" in a liquidity memo.

It costs about $0.60, and **I haven't run it yet**. So the prompt fixes above are untested against
Claude. The next run will show whether first drafts now pass or pay for a retry.

## What it cost

| | |
|---|---|
| Live memos on Lambda for this round | one, $0.0453 |
| Everything else here: signing, tracing, coverage, gold figures | $0 in model calls |
| Gold set, paid level (not yet run) | ~$0.60 |

Most of this round's work never called a model. The failures were in retrieval, extraction and
checking, and finding them took reading, not spending.

## What I'd tell someone deploying an agent

**Read one trace end to end before writing another check.** Every check I had came from a failure I
had predicted. The trace showed the ones I hadn't.

**Run new checks over old passes.** Three of twelve earlier memos had the problems. A check that only
runs forward tells you nothing about what already went out.

**Prefer a refusal to a plausible wrong number.** 0.18x for JPMorgan looked like an answer. "Doesn't
apply" is less satisfying and is the truth.

**Measure before fixing.** The memo I was going to shorten was already the right length.

---

*Built with Claude Code. The design decisions, review and evaluation are mine; most of the code is
model-written. 282 tests, none of which touch the network or an API key. Code:
[github.com/godot107/financial-analyst-agent](https://github.com/godot107/financial-analyst-agent)*

---

## Social drafts

**LinkedIn**

> I deployed my AI financial analyst to AWS Lambda, read the first trace of a production memo, and
> found four problems every check had passed.
>
> • Asked "How liquid is Microsoft?", the search matched the word *Microsoft* and handed the writer
> three paragraphs about Microsoft 365 revenue.
> • The writer cited two of them only to say they were irrelevant, and the AI judge called that
> "supported".
> • It repeated a value in exactly the pattern the prompt lists as bad.
> • It wrote "no longer covers near-term obligations": a rule of thumb, in words, where my digit
> check couldn't see it.
>
> Then I ran the new checks over twelve earlier memos that had passed. Three had the same problems.
>
> Extending it found more: JPMorgan came out at 0.18x debt to equity, wrong and plausible, because
> bank balance sheets don't use the tags I read. It now says those ratios don't apply.
>
> Lesson: checks come from failures you predicted. Traces show the ones you didn't.
>
> #AI #LLM #AWS #FinancialAnalysis

**X / Twitter**

> Deployed my "can't write numbers" AI analyst to Lambda and read the first trace.
>
> Every check passed. The memo still cited Microsoft 365 revenue paragraphs to answer "how liquid
> is Microsoft?", because the search matched the word "Microsoft". 🧵

> Also found:
> • the AI judge accepted a citation used only to dismiss a source
> • "no longer covers its obligations": a rule of thumb with no digits for the checker to catch
> • JPMorgan at 0.18x debt/equity: wrong, plausible, now refused
>
> 3 of 12 older memos had the same problems.
