# Agentic Financial Analyst

Ask a question about a company, and get a short memo built from its latest 10-K, where **every
number comes from the filing and none is written by the model**.

> Status: Iteration 1 in progress. See [`PLAN.md`](PLAN.md).

## How it works

```
START → plan → fetch → compute → write → check ──ok──→ render → END
                                   ▲        │
                                   └─retry──┘
```

- **plan** (Claude): picks which ratios answer the question.
- **fetch / compute** (Python): pulls the 10-K's financial statements and calculates the ratios.
- **write** (Claude): drafts the memo using placeholders like `{{roe:2025}}` instead of numbers.
- **check** (Python): rejects any draft where Claude typed a number itself.
- **render** (Python): fills in the real values.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # add ANTHROPIC_API_KEY and SEC_USER_AGENT
pytest
python -m fin_analyst MSFT "How liquid is Microsoft, and what drives its return on equity?"
```

## Results

_To come in Step 6: an example memo, the perturbation test, and cost per memo._

## Not investment advice

For education only. No price targets or buy/sell recommendations.
