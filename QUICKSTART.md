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
pytest                                              # 228 tests, no network, no API key
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

# read the last 3 10-Ks for four or five years of history (free: SEC data, cached per filing)
python -m fin_analyst COST "How has its leverage changed, counting leases?" --filings 3

# add the company's recent 8-K press releases (free, no key)
python -m fin_analyst MSFT "What has it announced about AI capacity?" --news

# ask follow-ups (up to 5, one shared budget)
python -m fin_analyst MSFT "How liquid is it?" --chat

# one question across a watchlist
python scripts/batch.py "How liquid is it?" MSFT COST JPM --max-usd 0.30

# filings are cached in cache/ by accession number; to fetch fresh:
python -m fin_analyst MSFT "How liquid is it?" --no-cache

# cheaper: skip the filing's narrative, or the claim check
python -m fin_analyst MSFT "How liquid is it?" --no-text --no-verify
```

## Run it as a service

```bash
# name=secret pairs; it refuses to start without one
export FIN_ANALYST_API_KEYS="me=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
python -m fin_analyst.server                          # http://127.0.0.1:8000/docs
```

```bash
KEY=...   # the secret part of FIN_ANALYST_API_KEYS
curl -s -X POST http://127.0.0.1:8000/v1/memos -H "X-API-Key: $KEY" \
     -H 'content-type: application/json' \
     -d '{"ticker": "MSFT", "question": "How liquid is it?"}'
# -> 202 {"id": "...", "status_url": "/v1/memos/...", "estimate_usd": 0.06}

curl -s http://127.0.0.1:8000/v1/memos/<id> -H "X-API-Key: $KEY"   # poll until done or failed
# add "reuse": true to get an earlier identical memo for $0.00, if the filing hasn't changed
curl -s http://127.0.0.1:8000/v1/coverage/MSFT -H "X-API-Key: $KEY" # free, immediate
```

Or as a container:

```bash
docker build -t fin-analyst .
docker run --rm -p 127.0.0.1:8000:8000 --env-file .env fin-analyst
```

Daily caps per key and overall live under `api:` in `config.yaml` ($1 and $3 by default), and are
checked when a job is submitted, before anything is spent.

To watch a memo being worked out step by step, including a summary of Claude's thinking, add
`--verbose` to any CLI run. Calling the AWS deployment is in [`docs/CALLING.md`](docs/CALLING.md).

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
