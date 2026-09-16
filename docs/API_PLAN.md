# Plan: serving the analyst as an API

Status: **Phase A complete. Phase B built, not yet deployed** (templates, scripts and Lambda image
tested offline). Phase C planned. Iteration 3. The CLI and batch runner work today; this plans the
same workflow behind an HTTP interface so other systems can ask for memos.

## What the service is, and is not

It returns memos: figures from the filing, explanations cited and checked. It is **advisory**. A
caller may show a memo to a person, log it, or attach it to an alert. **A caller must never wait on
a memo to make a decision, and must never let one change a decision** until that effect has been
tested out of sample. A memo takes tens of seconds, costs money, and can fail closed by design; none
of that belongs in a decision path.

## Three facts that shape the design

**A memo is too slow for a synchronous gateway.** A memo takes roughly 30–90 seconds (two or three
Claude calls, an SEC fetch, optionally a retry). API Gateway HTTP APIs have a maximum integration
timeout of **30 seconds, which cannot be increased** (checked against the AWS quota page,
2026-09-16). So the API accepts a job and returns immediately; the memo is collected later.

**Every request spends money.** An unauthenticated endpoint is an open tab on the Anthropic account.
Authentication, a per-caller daily cap, and a global daily cap are requirements, not hardening.

**The SEC's fair-access rules apply to a server too.** Every request identifies itself
(`SEC_USER_AGENT`) and stays under 10 requests a second, so batch work runs serially.

## API surface (v1)

| Method | Path | What it does | Cost |
|---|---|---|---|
| `POST` | `/v1/memos` | Submit `{ticker, question, peer?, news?, market?, verify?}`. Returns `202` with a job id, a status URL, and a cost estimate. | a memo |
| `GET` | `/v1/memos/{id}` | `queued`, `running`, `done` (memo + run record + cost) or `failed` (the reason, e.g. "gave up after 3 drafts"). | free |
| `POST` | `/v1/batches` | One question across tickers, with a required `max_usd`. | a memo each |
| `GET` | `/v1/coverage/{ticker}` | Which tags matched and which ratios the filing supports. Synchronous: no model. | free |
| `GET` | `/v1/health` | Liveness, plus today's spend against the caps. | free |

An optional `notify` field on submission (a webhook URL, or a Telegram chat) delivers the finished
memo, so a caller can fire and forget instead of polling.

A failed memo is a normal response, not a server error. "Gave up after 3 drafts" means the model
kept writing numbers itself and nothing was published — the design working.

## Phases

### Phase A — local service (free to run, apart from the memos) ✅

- FastAPI app exposing the routes above over the existing `run_analysis` / `run_batch` / coverage
  code. No change to the workflow itself.
- A single background worker and a SQLite job table: one memo at a time, which also keeps SEC
  traffic serial.
- API-key auth (header), a per-key daily dollar cap, and a global daily cap, all enforced before a
  job is accepted rather than after it has spent.
- Tests with the fake analyst, as everywhere else: no network, no key. Covers: job lifecycle, cap
  refusal (`429` with the reset time), auth refusal, a failed memo reported as `done`/`failed`
  rather than `500`.
- A Dockerfile, so Phase B deploys the image that Phase A tested.

**Done when:** a local client submits a memo, polls it to completion, and a request over the cap is
refused before any Claude call is made.

**As built:** `fin_analyst/service.py` (routes and worker), `fin_analyst/jobs.py` (SQLite ledger),
`fin_analyst/server.py` (entry point), a `Dockerfile`. 18 tests drive it with the fake analyst.
Caps count a queued job at its **estimate** and a finished one at its **real cost**; counting only
finished jobs would let a burst of submissions all pass the check and then all spend. A refusal is a
`429` naming the cap, the remaining allowance and the reset time, and the tests assert no analyst
was even constructed. Someone else's job id returns `404`, not `403`, so an id confirms nothing.

The image (813 MB) was smoke-tested without spending: no `.env` inside it, runs as a non-root user,
exits with a clear message when `FIN_ANALYST_API_KEYS` is missing or a secret is under 16
characters, answers `401` without a key and `422` for a malformed ticker, and serves the free
coverage route live from EDGAR.

**Done-when met (2026-09-16):** one real memo through the container. Submission answered `202` in
under a second; the job finished after 27 seconds as `done`, one draft, two cited claims checked,
**$0.0491** against a $0.06 estimate. `/v1/health` then reported $0.0491 committed — the ledger had
replaced the estimate with the real cost, as designed.

Note for Phase B: the image is large (pandas, edgartools, LangGraph), so expect cold starts of
several seconds on Lambda. That is harmless here — submission and work are separate invocations.

### Phase B — AWS, private to the account

- **Container-image Lambda** (arm64) running the same image. Two entry points in one image: a thin
  handler that validates, checks caps, records the job and returns `202`, and a worker invoked
  asynchronously (`InvocationType=Event`) that runs the memo and writes the result.
- **Lambda function URL with `AWS_IAM` auth**, not API Gateway. Callers are the owner's own
  services, which already hold IAM credentials, so SigV4 signing costs nothing extra and nothing is
  publicly reachable. The submission handler returns in well under a second, so the 30-second limit
  never applies.
- **State:** job records and memos in S3 (one object per job; a DynamoDB table only if listing
  and querying become necessary). Spend counters alongside.
- **Secrets** (`ANTHROPIC_API_KEY`, `SEC_USER_AGENT`, `ALPHAVANTAGE_KEY`) in SSM Parameter Store
  as SecureStrings, read at cold start.
- **Infrastructure as CloudFormation**, a least-privilege deployer role, and a teardown script.
- **Cost guards before the first deploy:** an AWS Budgets alarm, log retention set on every log
  group, an ECR lifecycle rule, and a CloudBurn IaC scan in CI (`--fail-on high`).

**Done when:** a signed request from another service in the account gets a memo back through the
notify hook, and teardown removes everything.

**As built:**

```
infra/iam.yaml   admin, once: deployer user, permissions boundary, budget alarm
infra/ecr.yaml   image repository (lifecycle rules, scan on push, emptied on delete)
infra/app.yaml   Lambda + IAM function URL + S3 jobs bucket + log group + role

deploy/00_iam.sh      admin: bootstrap stack; deployer key written to a CLI profile, never printed
deploy/01_secrets.sh  .env -> SSM SecureStrings, values on stdin, never on the command line
deploy/02_ecr.sh      repository
deploy/03_image.sh    build (linux/amd64, --provenance=false) and push, tagged with the commit
deploy/04_app.sh      previews the change set; --execute applies it
deploy/05_verify.sh   free: unsigned -> 403, signed without key -> 401, health, coverage
                      --memo: one real memo (~$0.05)
deploy/99_teardown.sh service, repository, secrets; --iam also the bootstrap (as admin)
```

Code: `fin_analyst/lambda_handler.py` (routes a function-URL request to the app through Mangum, and
a `{"job_id"}` event to the worker) and `S3JobStore` beside the SQLite one. 9 more tests with fake
AWS clients. The image was invoked locally through Lambda's runtime emulator: a job event reached the
worker, a function-URL request reached the app and was refused `401` without a key, cold start
6.2 s, warm 15 ms.

Decisions worth knowing:

- **The deployer cannot escalate.** It may create roles only if they carry the `fin-analyst-boundary`
  policy, may never remove a boundary, and may pass roles only to Lambda. Its CloudFormation grant
  matches only `fin-analyst-ecr` and `fin-analyst-app`, so it cannot touch the bootstrap stack.
  `00_iam.sh` checks both after creating it.
- **Retries are off for the worker.** Lambda retries asynchronous invocations twice by default, and a
  retried memo is a second bill. A failed job stays failed and says why. Delivery is still
  at-least-once, so a job is claimed only while `queued`: an event delivered twice runs once.
- **x86_64, not arm64.** The build machine has no arm64 emulation. CloudBurn flags it; the compute
  difference is negligible next to the model cost, so it is accepted in `.cloudburn.yml`.
- **The budget alarm is account-wide** and created before anything billable. It alerts; it does not
  stop spending. The service's own caps are what stop it.

### Phase C — public endpoint (optional, and last)

Only if there is a reason for strangers to call it. A custom domain in front of API Gateway (the
fast routes only), keys issued per caller, rate limits, and a public tier restricted to ratios-only
memos (`--no-text --no-verify`, ~$0.03) with a small global daily cap. A web application firewall
adds a monthly charge; price it before adding it.

## Caching

Measured first, because it changes what is worth caching. SEC fetches for a memo took **3.6 s cold
and 1.1 s warm**; the memo itself took **27 s and $0.049**. SEC data is free, so caching filings
saves time and SEC traffic but no money. The money is in not writing the same memo twice.

| What | Key | Why it is never stale | Kept |
|---|---|---|---|
| Facts and 10-K text | accession number | a filing never changes; an amendment is a new accession number | 365 days |
| 8-K releases | accession number, each | same | 365 days |
| Share price | ticker + day | a daily close | 7 days |
| Finished memo | hash of question, options, accession numbers, code fingerprint (+ day if it used news or a price) | anything that could change the memo changes the key | 90 days |

- **Which filing is latest is looked up every time.** Only a filing's content is cached, so a new
  10-K is never hidden behind an old one.
- **The code fingerprint is a hash of the source** that writes and checks memos, plus `config.yaml`.
  A changed prompt stops every old key matching without anyone remembering to bump a version.
- **Memo reuse is opt-in** (`"reuse": true`). Every published memo is kept either way; a failed one
  never is. A reused memo costs $0.00 and names the job it came from.
- **Where:** a `cache/` folder locally, the same bucket's `cache/` prefix on Lambda, with lifecycle
  rules per prefix. S3 because it costs nothing idle and the bucket already exists; ElastiCache bills
  hourly, EFS needs a VPC and so a NAT gateway (~$32/month) to reach SEC and Anthropic, and Lambda's
  `/tmp` is wiped on every cold start.

Measured locally on Costco: 2.4 s to fetch facts and text the first time, under a hundredth of a
second the second.

## Rough costs

| | Per memo |
|---|---|
| Claude calls | $0.03–0.09 (measured) |
| Lambda compute, SEC fetches, S3 | a fraction of a cent (estimate; verify against current AWS pricing) |

The model dominates. A daily batch over ten tickers is roughly **$0.60 a day, ~$18 a month**, so
scheduled batches should default to a short list or a weekly cadence, with `max_usd` required.

## Client integration pattern

```python
# In the caller: never awaited by anything that decides.
submit_memo(ticker, question, notify=alert_channel)   # returns in < 1s, or fails silently
```

- **Fire and forget.** The call happens after the decision is made and executed, never before.
- **Fail open for the caller.** If the service is down, the caller's job continues unchanged; the
  failure is logged, not raised.
- **Keep the analyst out of backtests.** The model has read about most of history. A memo generated
  inside a historical simulation is recall, not analysis, and would contaminate the test.
- **Mirror the isolation test** in the caller: its decision code may not import the analyst client.

## Open questions

- Whether news should come from a source the caller already pays for (for example a broker's news
  API) rather than 8-K exhibits alone.
- Whether one memo per event is too many: a digest per day may be more useful and cheaper.
