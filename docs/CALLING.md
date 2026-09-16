# Calling the deployed service, and watching it think

The service on Lambda sits behind a **function URL with `AuthType: AWS_IAM`**. There are two locks,
and a request needs both keys:

| Lock | Checked by | Missing it gets you |
|---|---|---|
| An **AWS SigV4 signature**, from credentials allowed to call `lambda:InvokeFunctionUrl` | AWS, before any of this code runs | `403 {"Message":"Forbidden"}` |
| An **`X-API-Key`** header, from `FIN_ANALYST_API_KEYS` | `service.py` | `401` |

The signature says *this caller may reach the function at all*. The API key says *which caller this
is*, so the daily spending caps can be charged to them.

## What signing is

SigV4 doesn't send your secret. It builds a **canonical request** (method, path, query, the
headers being signed, and a SHA-256 of the body), then signs it with a key derived from your secret,
the date, the region and the service (`lambda`). It adds three headers:

```text
X-Amz-Date: 20260916T215454Z
X-Amz-Security-Token: ...        (only for temporary credentials)
Authorization: AWS4-HMAC-SHA256 Credential=AKIA…/20260916/us-east-1/lambda/aws4_request,
               SignedHeaders=content-type;host;x-amz-date;x-api-key, Signature=9f90d6…
```

AWS repeats the calculation. If anything changed in transit (the path, a signed header, one byte of
the body), the signatures don't match. A signature is accepted for **5 minutes**, so a captured
request can't be replayed later.

## 1. The helper script

It finds the URL and your API key, and signs with the `fin-analyst` profile:

```bash
./deploy/call.sh GET /v1/health                      # free
./deploy/call.sh GET /v1/coverage/COST               # free: EDGAR only, no model
./deploy/call.sh --explain GET /v1/health            # show the signed headers (masked)

./deploy/call.sh POST /v1/memos '{"ticker": "MSFT", "question": "How liquid is Microsoft?"}'
# HTTP 202 {"id": "20260916…", "status_url": "/v1/memos/20260916…", "estimate_usd": 0.06}
./deploy/call.sh wait 20260916…                      # polls, then prints the trace and memo
```

A memo costs about $0.05. Add `"reuse": true` to get an earlier identical memo for $0.00 if
the filing hasn't changed.

## 2. curl

curl 7.75+ signs requests itself with `--aws-sigv4`. Pass the credentials through a config file read
from stdin (`-K -`), not with `--user` on the command line, where other processes on the machine can
read them:

```bash
URL=https://<id>.lambda-url.us-east-1.on.aws
KEY=...   # the secret half of FIN_ANALYST_API_KEYS in .env
eval "$(aws configure export-credentials --profile fin-analyst --format env)"

creds() { printf 'user = "%s:%s"\nheader = "x-api-key: %s"\n' \
            "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" "$KEY"; }

creds | curl -s -K - --aws-sigv4 "aws:amz:us-east-1:lambda" "$URL/v1/health"

creds | curl -s -K - --aws-sigv4 "aws:amz:us-east-1:lambda" \
     -X POST -H 'content-type: application/json' \
     --data '{"ticker": "MSFT", "question": "How liquid is Microsoft?"}' \
     "$URL/v1/memos"

curl -s "$URL/v1/health"     # unsigned: {"Message":"Forbidden"}
```

With temporary credentials (an assumed role, SSO), also send
`-H "x-amz-security-token: $AWS_SESSION_TOKEN"`. `aws:amz:REGION:SERVICE` names what the signature
is for; the service is `lambda` for a function URL.

## 3. Python, as another program would

```python
import json, urllib.request
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

def call(method, url, api_key, body=None, region="us-east-1"):
    data = json.dumps(body).encode() if body is not None else None
    request = AWSRequest(method=method, url=url, data=data,
                         headers={"content-type": "application/json", "x-api-key": api_key})
    SigV4Auth(boto3.Session().get_credentials(), "lambda", region).add_auth(request)
    sent = urllib.request.Request(url, data=data, headers=dict(request.headers), method=method)
    with urllib.request.urlopen(sent, timeout=30) as response:
        return json.loads(response.read())
```

Sign exactly the bytes you send. Serializing the body a second time after signing breaks the
signature.

# Watching the agent think

Every run keeps a **trace**: each step, how long it took, what it produced, and every Claude call
with its tokens, cost, stop reason and **a summary of its thinking**. Claude Opus 5 never returns
raw thinking. The request asks for `display: "summarized"` (`thinking_summaries` in `config.yaml`).
Thinking is billed in full either way, so the summary adds nothing to the cost.

A run looks like this:

```text
[   0.0s] run      start  ticker=MSFT  question=How liquid is Microsoft?  text=True  verify=True
[   3.1s] claude   plan  model=claude-opus-5  effort=low  input_tokens=…  cost_usd=…  stop_reason=tool_use
           thinking:
             …why these ratios answer the question…
[   3.1s] plan     done  metric_ids=current_ratio, quick_ratio, cash_flow_ratio
[   4.0s] fetch    done  facts=35  fiscal_years=2024, 2025, 2026
[   4.0s] compute  done
           values:
             - current_ratio:2026 = 1.2303
[   4.2s] retrieve done
           passages:
             - [P1] Item 7: …
[  18.9s] claude   write  …
           thinking: …
[  18.9s] write    done  attempt=1
           draft: …the placeholder draft, before any number is filled in…
[  18.9s] check    done
[  18.9s] route    check -> verify  problems=0
[  24.6s] verify   done
           verdicts:
             - supported ['P2']: …
[  24.6s] route    verify -> render  problems=0
[  24.6s] run      done  drafts=1  cost_usd=0.0491
```

The drafts, the checker's objections and the retry decisions (`route check -> write`) are where the
workflow's behaviour shows, alongside what Claude was thinking.

## Where to see it

| Where | How |
|---|---|
| CLI, live | `python -m fin_analyst MSFT "How liquid is Microsoft?" --verbose` |
| CLI, afterwards | the `trace` list in `runs/MSFT-<time>.json` |
| Local service, live | printed in the terminal running `python -m fin_analyst.server` |
| API, afterwards | `result.trace` in `GET /v1/memos/{id}`, or `./deploy/call.sh wait <id>` |
| Lambda, live | CloudWatch: one JSON line per event, tagged with `job_id` |

On Lambda:

```bash
aws logs tail /aws/lambda/fin-analyst --follow --format short --profile fin-analyst
```

Or in CloudWatch Logs Insights, one job's steps in order:

```text
fields @timestamp, trace.step, trace.event, trace.data.thinking, trace.data.draft
| filter job_id = "20260916…"
| sort @timestamp asc
```

Logs are kept 14 days (`LogRetentionDays` in `infra/app.yaml`). A trace is a few kilobytes, so the
CloudWatch ingestion charge ($0.50/GB) rounds to nothing at this volume.
