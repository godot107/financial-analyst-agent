# The stack on AWS

One Lambda function, one S3 bucket, and nothing that runs when nobody is asking. The whole thing
costs a few cents a month at rest; the money is in the Claude calls, which is why the spending caps
sit in front of them.

Everything here is created by CloudFormation from `infra/`, deployed by the numbered scripts in
`deploy/`. Calling it is [`CALLING.md`](CALLING.md); the plan behind it is
[`API_PLAN.md`](API_PLAN.md).

## The architecture

*(GitHub renders the diagram below; [`architecture.png`](architecture.png) is the same picture as an
image, for anywhere that doesn't.)*

```mermaid
flowchart LR
    caller["<b>Caller</b><br/>deploy/call.sh, curl, AuraTrade<br/>SigV4 signature + X-API-Key"]

    subgraph aws["AWS account, us-east-1"]
        direction TB
        url(["Lambda function URL<br/>AuthType: AWS_IAM"])

        subgraph fn["Lambda: fin-analyst &mdash; one container image, 2 GB, 300 s"]
            direction LR
            api["<b>API</b> (FastAPI via Mangum)<br/>submit, poll, coverage, health<br/>checks the daily caps"]
            worker["<b>Worker</b><br/>the LangGraph workflow:<br/>plan, fetch, compute, retrieve,<br/>write, check, verify, render"]
        end

        subgraph state["State and secrets"]
            direction TB
            s3[("S3 fin-analyst-jobs-…<br/>jobs/ the ledger and today's spend<br/>cache/ filings, prices, memos")]
            ssm[["SSM Parameter Store<br/>SecureString + KMS<br/>read once per cold start"]]
            ecr[("ECR<br/>the image, tagged by commit")]
        end

        subgraph watch["Telling someone it stopped"]
            direction LR
            logs["CloudWatch Logs<br/>one JSON line per trace event"]
            alarms{{"2 alarms<br/>out of credit · function errors"}}
            sns(["SNS fin-analyst-alerts"])
        end
    end

    subgraph outside["Outside AWS"]
        direction TB
        edgar["SEC EDGAR<br/>10-K XBRL, Item 7 and 1A"]
        claude["Anthropic API<br/>claude-opus-5"]
        av["Alpha Vantage<br/>share price, optional"]
    end

    email["aws@willieman.com"]

    caller ==>|"HTTPS"| url ==> api
    api -.->|"202 with a job id"| caller
    api ==>|"asynchronous self-invoke"| worker
    api <--> s3
    worker <-->|"jobs, cache"| s3
    ecr -.-> fn
    ssm -.-> fn
    worker -->|"facts and narrative"| edgar
    worker -->|"plan · write · verify"| claude
    worker -->|"one price, when asked"| av
    fn --> logs --> alarms --> sns --> email

    classDef ext fill:#ffffff,stroke:#999,stroke-dasharray:5 4,color:#333
    class caller,edgar,claude,av,email ext
```

**Why a function URL rather than API Gateway.** A memo takes 20 seconds to a minute, and API
Gateway stops waiting at 30. The submission returns immediately either way, so the gateway would
add a hop, a cost and a second thing to configure for nothing. The tradeoff is that a function URL
has no custom domain of its own: that needs API Gateway in front, which is
[tabled](API_PLAN.md#phase-c--public-endpoint-optional-and-last).

**Why the function invokes itself.** The memo has to outlive the request. The API answers with a
job id in well under a second, then invokes the same function asynchronously with that id; the
second invocation runs the workflow and writes the result to S3. One function, one image, one set
of permissions — and the worker gets Lambda's own retry and timeout behaviour for free.

## What happens on one memo

```mermaid
sequenceDiagram
    autonumber
    participant C as Caller
    participant U as Function URL
    participant A as API (Lambda)
    participant W as Worker (Lambda)
    participant S as S3
    participant E as SEC EDGAR
    participant K as Claude

    C->>U: POST /v1/memos (signed + API key)
    U->>A: request
    A->>A: check the caller's daily cap, before any spend
    A->>S: write the queued job
    A-->>W: invoke asynchronously (job id)
    A-->>C: 202 { id, estimate_usd }
    W->>S: claim the job
    W->>K: plan: which ratios answer this?
    W->>E: the latest 10-K's facts and narrative
    Note over W: Python computes every ratio
    W->>K: write: a draft in placeholders
    W->>W: check: leaked digits, bad citations, rules of thumb
    W->>K: verify: is each cited claim in its passage?
    W->>S: memo, structured result and trace
    C->>U: GET /v1/memos/{id} (polling)
    U->>A: request
    A->>S: read the job
    A-->>C: done: memo, cost, metrics, passages, trace
```

A failed check sends the draft back to `write`; three failures publish nothing and record why.

## What each piece is for

| Resource | Why it exists | What it costs |
|---|---|---|
| **Lambda** `fin-analyst` | the API and the worker, one container image | per request; idle costs nothing |
| **Function URL**, `AWS_IAM` | the front door: AWS checks the SigV4 signature before any code runs | free |
| **ECR** repository | the image, tagged with the commit it was built from | ~$0.10/GB/month |
| **S3** `fin-analyst-jobs-…` | `jobs/` the ledger and today's spend; `cache/` filings by accession, prices by day, memos by hash | cents |
| **SSM Parameter Store** | the Anthropic key, the API keys, the SEC identity: SecureString, read at cold start, never in the image | free |
| **CloudWatch Logs** | one JSON line per trace event, 14-day retention | cents |
| **SNS + 2 alarms** | tells someone when the account runs out of credit or the function errors | ~$0.20/month |
| **IAM role + permissions boundary** | the function may touch only this project's bucket, parameters and log group | free |

The costs that matter are Claude's: about $0.03–0.09 a memo, capped per run, per caller per day,
and across the service per day.

## Who may do what

```mermaid
flowchart TB
    subgraph admin["Admin (root, aws login)"]
        iam["deploy/00_iam.sh<br/>fin-analyst-iam stack"]
    end
    subgraph deployer["fin-analyst deployer (an access key)"]
        rest["01_secrets → 02_ecr → 03_image → 04_app → 05_verify"]
    end
    boundary["Permissions boundary<br/>the ceiling on any role the deployer creates"]
    role["Function role<br/>this project's bucket, parameters, log group,<br/>and invoking itself"]

    iam --> boundary
    iam --> deployer
    rest --> role
    boundary -. limits .-> role
```

Two locks guard the service itself, and a request needs both: an **AWS SigV4 signature**, checked
by AWS before any of this code runs, and an **`X-API-Key`**, checked by the app so a daily spending
cap can be charged to the caller who used it. The details, and how to sign a request by hand, are
in [`CALLING.md`](CALLING.md).

## When it breaks

| What happens | Where it shows up | Does it alert? |
|---|---|---|
| The Anthropic account is out of credit | every run fails on its first call, spending nothing | **yes**, by email |
| The function crashes, times out, or a deploy is bad | Lambda's `Errors` metric | **yes**, by email |
| A memo can't pass its own checks | the job's `error`, and the trace | no: this is the design working |
| A ratio can't be computed | `"not available"` with the reason, in the memo | no |
| EDGAR or Alpha Vantage is unreachable | the job fails, or the valuation ratios report themselves unavailable | no |

Nothing here is investment advice, and a caller must never wait on a memo to make a decision.
