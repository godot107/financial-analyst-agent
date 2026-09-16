# Quick start

Five minutes to your first memo. It needs Python 3.12+, an Anthropic API key, and an email address
the SEC can contact you at.

## 1. Install

```bash
git clone https://github.com/<you>/financial-analyst-agent.git
cd financial-analyst-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Two settings

```bash
cp .env.example .env
```

Open `.env` and fill in both lines:

```
SEC_USER_AGENT=Your Name you@example.com
ANTHROPIC_API_KEY=sk-ant-...
```

`SEC_USER_AGENT` is not a key and costs nothing. The SEC's fair-access rules require every
automated request to identify itself, and requests without it are refused. `ANTHROPIC_API_KEY`
comes from [the Anthropic Console](https://console.anthropic.com/) and is billed per use — a memo
costs a few cents.

## 3. Check the setup without spending anything

```bash
pytest                                              # 170 tests, no network, no API key
python -m fin_analyst MSFT "How liquid is it?" --dry-run
python scripts/coverage.py MSFT                     # reads the filing, calls no model
```

## 4. Write a memo

```bash
python -m fin_analyst MSFT "How liquid is Microsoft, and what drives its return on equity?"
```

About $0.03–0.09 and half a minute. The memo prints, and a copy plus a full run record lands in
`runs/`.

## Optional: valuation ratios

P/E, market-to-book and EV/revenue need a share price, which no filing contains. A free key from
[alphavantage.co](https://www.alphavantage.co/support/#api-key) (25 requests a day) enables them:

```
ALPHAVANTAGE_KEY=...
```

```bash
python -m fin_analyst MSFT "Is it expensive relative to its earnings?" --market
```

The price comes from Alpha Vantage; the share count comes from the filing. Their fundamentals are
deliberately unused — no accession number, so a figure from them could not be traced to a filing.
Without the key the memo still works; the valuation ratios report themselves unavailable.

## Everything else

```bash
# explain a movement, citing the filing's own words
python -m fin_analyst MSFT "Why did gross margin change?"

# compare two companies
python -m fin_analyst MSFT "How does its liquidity compare?" --peer GOOGL

# add the company's recent 8-K press releases (free, no key)
python -m fin_analyst MSFT "What has it announced about AI capacity?" --news

# ask follow-ups (up to 5, one shared budget)
python -m fin_analyst MSFT "How liquid is it?" --chat

# one question across a watchlist
python scripts/batch.py "How liquid is it?" MSFT COST JPM --max-usd 0.30

# cheaper: skip the filing's narrative, or the claim check
python -m fin_analyst MSFT "How liquid is it?" --no-text --no-verify
```

## When something goes wrong

| What you see | What it means |
|---|---|
| `Command 'python' not found` | The virtualenv isn't active. Run `source .venv/bin/activate`, or call `.venv/bin/python` directly. |
| `No module named fin_analyst` | You're in the wrong folder. Run from the project root, the one holding `fin_analyst/`. |
| `No ANTHROPIC_API_KEY found` | Add it to `.env`, or run `ant auth login`. |
| `the API rejected the key` | The key is wrong, revoked, or truncated. Check it in the Console. |
| `SEC_USER_AGENT is not set` | Add your name and email to `.env`; the SEC requires it. |
| `403` from the SEC | Same cause: identify yourself in `SEC_USER_AGENT`. |
| `gave up after 3 drafts` | Working as intended. The model kept writing numbers itself, so nothing was published. The run record says what it wrote. |

## What it costs

| Run | Typical |
|---|---|
| Memo, ratios only (`--no-text`) | ~$0.03 |
| Memo with citations and the claim check | $0.04–0.09 |
| A retry (rejected draft) | +$0.03 |
| `coverage.py`, `--dry-run`, `pytest` | free |

A per-run cap in `config.yaml` (`max_usd_per_run`, $1 by default) stops a runaway. It is a guard,
not a budget.
