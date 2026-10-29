# Plan: the same analyst on Amazon Bedrock AgentCore, measured against the Lambda stack

Status: **Phase 0 built (2026-10-29, $0); nothing deployed or spent.** Drafted 2026-10-29;
revised the same day: **models through Amazon Bedrock, not the Anthropic API; all infrastructure in
CloudFormation, tagged.** Phase 0 results are at the end of this file.

## The question

The memo service already runs on AWS, but every piece of the platform around the agent is
hand-built: a Lambda that invokes itself for async jobs, an S3 job ledger, daily spend caps in
the API, secrets in SSM, an API key plus SigV4 for callers, `trace.py` for the run record, and
`evals/gold.py` for quality. AgentCore sells most of those as managed services, and Bedrock
removes the external model API and its key.

> Which of the hand-built pieces does AgentCore actually replace, what does each one cost in
> money, latency and lock-in, and how good are the memos on the Claude models Bedrock offers
> this account?

The answer is a write-up either way: "moved, here's what got simpler" or "kept Lambda, here's
why", with the numbers. It's also the reference design for a pattern used elsewhere in this
workspace: **an LLM proposes, a checker critiques, code has the final word, and every step is
recorded.**

## Two changes, measured separately

This plan changes two things: the **model provider** (Anthropic API → Bedrock, which also changes
the model) and the **platform** (Lambda → AgentCore). Measured together, a worse memo couldn't be
blamed on either. So:

1. **Model first, locally:** the same graph on the same machine, Anthropic API vs Bedrock. Any
   difference is the model's.
2. **Platform second:** Bedrock locally vs Bedrock on AgentCore Runtime. Same model, so any
   difference is the platform's.

## The model on Bedrock

Today's memo model is `claude-opus-5` through the Anthropic API. On Bedrock, this account's
model agreements allow only **Claude Opus 4.5** and **Claude Haiku 4.5** (checked 2026-10-29 with
`aws bedrock get-foundation-model-availability`: Opus 5, Sonnet 4.5–5.5 and Haiku 5.5 are
`NOT_AVAILABLE`; Opus 4.5 and Haiku 4.5 are `AVAILABLE`).

- **Opus 4.5 replaces Opus 5** for plan, write and verify: the closest model available. The memo
  will differ from today's because the model differs, so parity is measured by the gold set's
  scores, not by identical text.
- **Haiku 4.5 is an optional cheaper arm**, scored on the same gold set. Switching the default
  to it is Willie's call, as with any model change (`CLAUDE.md`).
- **The `anthropic` SDK stays.** Its `AnthropicBedrock` client calls Bedrock with the same
  Messages API and the Runtime's IAM role, so `llm.py` keeps its explicit calls and the
  LLM-isolation test keeps holding. The client is chosen by a `provider: anthropic | bedrock`
  setting in `config.yaml`; tests use the fake client as today.
- **Calls go through tagged application inference profiles** (`AWS::Bedrock::ApplicationInferenceProfile`),
  one per model, so Bedrock spend shows up under `project=fin-analyst`. On-demand Bedrock calls
  can't be tagged any other way. (robinhood-agent uses the same setup.)
- To check in Phase 0: Opus 4.5 on Bedrock may not support everything the code uses today
  (adaptive thinking, the `effort` setting, strict tool schemas, refusal fallbacks). Each gap
  gets the nearest equivalent (e.g. a fixed thinking budget), recorded here.

## What must not change

- **The graph.** plan → fetch → compute → retrieve → write → check → verify → render stays a fixed
  LangGraph pipeline. AgentCore hosts it; it doesn't redesign it.
- **Claude never writes a number.** Placeholders, the leak check and the claim check stay in the
  graph, in Python, exactly as today. Nothing in AgentCore replaces them.
- **The run record** (`service.structured_result` + `trace.py`) stays the contract. AgentCore
  Observability is extra, not a replacement: programs read the run record, not CloudWatch. Cost
  per call comes from Bedrock's token counts and `config.yaml` prices for the Bedrock models.
- **Tests stay offline** (`tests/conftest.py` blocks sockets). AgentCore code lives in one
  adapter module (like `lambda_handler.py`); the graph imports nothing from it.
- **The Lambda stack keeps running, on the Anthropic API,** until the decision at the end. The
  existing caller stays on it; nothing switches over mid-experiment.
- **House rules:** ask before any paid run, with the estimated cost; the budget guard stays on.

## Infrastructure: CloudFormation, tagged

Everything is a CloudFormation stack deployed by numbered scripts, as `infra/` + `deploy/` are
today. No console clicks, no starter-toolkit deploys (the toolkit can be used locally in Phase 0
only).

| Stack | Who deploys | Resources |
|---|---|---|
| `fin-analyst-agentcore-iam` (`infra/agentcore/iam.yaml`) | admin, once (`deploy/agentcore/00_iam.sh`) | The two tagged `AWS::Bedrock::ApplicationInferenceProfile`s (Opus 4.5, Haiku 4.5); the Runtime role (Bedrock invoke on exactly those profiles, the global profiles they copy and the models behind them; its own S3 bucket; its own ECR repo; logs, traces, metrics; Identity for Phase 3); the CodeBuild role; a deploy policy added to the existing restricted deployer; AWS Budget `fin-analyst-agentcore` $5, filtered to `component=agentcore` |
| `fin-analyst-agentcore-build` (`build.yaml`) | deployer (`01_build.sh`) | Its own ECR repo `fin-analyst-agentcore` (the Lambda repo keeps only five images), a source bucket, a CodeBuild project on ARM that builds `Dockerfile.agentcore` for arm64 |
| `fin-analyst-agentcore-app` (`app.yaml`) | deployer (`02_app.sh`, previews unless `--execute`) | `AWS::BedrockAgentCore::Runtime` (container, HTTP, public network, 300 s idle timeout, no secrets in its environment) + `RuntimeEndpoint` `live`, and its own jobs bucket |
| `-tools` (Phase 3), `-evals` (Phase 4) | deployer | `Gateway`, `GatewayTarget`s, `PolicyEngine` + `Policy`; `Evaluator`s, `OnlineEvaluationConfig` |

**Tags** (`shared/TAGGING.md`), on every stack and every resource that takes them:
`project=fin-analyst environment=dev owner=williemaize cost-center=personal managed-by=cloudformation
repository=github.com/godot107/financial-analyst-agent`, plus `component=agentcore` (a new
component value, added to `TAGGING.md` with this work). `environment=dev` because this is an
experiment next to the `prod` Lambda stack; it becomes `prod` only if the decision rule says move.

Checked 2026-10-29 with `aws cloudformation describe-type`: `Runtime`, `RuntimeEndpoint`, `Gateway`,
`PolicyEngine`, `Evaluator`, `OnlineEvaluationConfig`, `WorkloadIdentity` and
`ApplicationInferenceProfile` all have a `Tags` property; **`GatewayTarget` and `Policy` don't**
(they live inside a tagged Gateway / policy engine). After each deploy, a check lists every
resource in the stacks and reports any without the required tags. The CloudBurn IaC scan runs on
the new templates in CI, like the existing ones.

## AgentCore pieces, one decision each

| Today (hand-built) | AgentCore / Bedrock piece | Decision | Why |
|---|---|---|---|
| Lambda + function URL + self-invoke for async jobs; 300 s limit | **Runtime** | **Adopt and measure** | The main platform test. Could remove the self-invoke trick and the 300 s ceiling. Compare cold start, latency, cost at rest |
| Anthropic API key in SSM; X-API-Key + SigV4 for callers | **Bedrock** (IAM, no model key) + **Identity** | **Adopt** | The model key disappears: the Runtime role invokes Bedrock. Only the Alpha Vantage key remains, in the credential provider. Caller auth becomes IAM/OAuth on the Runtime endpoint |
| `trace.py` → run record + CloudWatch JSON lines | **Observability** | **Adopt alongside** | An extra view of each node and call. The run record stays the source of truth |
| `evals/gold.py` (77 free checks + paid set), claim check graded 8/8 by hand | **Evaluations** | **Adopt for monitoring** | Gold checks become code-based evaluators. Built-in evaluators run on a sample of memos; compare their faithfulness scores with the claim check's hand labels. Custom LLM judges must be Bedrock models: Haiku 4.5 is available, so a Claude judge is possible |
| Tools are Python functions inside the graph; the Strands chat (`--chat-mode strands`) calls them as tools | **Gateway** | **Adopt for the chat agent and for memo-as-a-tool only** | The fixed pipeline gains nothing from calling its own functions over the network. Two real uses: the Strands chat agent's tools, and the memo service itself as one MCP tool other agents can call |
| Pydantic limits, request caps (`max_usd`, filings ≤ 5), daily spend caps in the API | **Policy** | **Test** | A second fence, not a replacement: ticker format, `filings ≤ 5`, `max_usd ≤ cap`, market tool only when asked. Measured with a red-team set of bad tool calls. Daily spend caps need state, so they stay in code. Confirm Policy's GA status first (AWS's pages disagree) |
| Memos cached by hash; chat state in memory | **Memory** | **Skip** | Memos are stateless by design; long-term memory would make runs less reproducible |
| — | Code Interpreter, Browser | **Skip** | Numbers come from pre-written Python, never generated code; no web browsing |

## Phases

Each phase ends with a commit, a short results note in this file, and a Trello comment.

0. **Offline spike ($0).** `provider: bedrock` in `llm.py` with the `AnthropicBedrock` client,
   tested with the fake client. List which Opus 4.5 settings differ from Opus 5 (thinking, effort,
   strict tools, refusals). Wrap `run_memo` in the Runtime entrypoint and run it in a local
   container against fixtures. Draft the CloudFormation templates; `cfn-lint` and the CloudBurn
   scan pass. Look up current AgentCore prices on the AWS pricing page.
1. **Model swap, locally (paid, ask first).** Deploy only `-iam` and `-models` (profiles + budget).
   Run the paid gold set on Bedrock Opus 4.5 and compare with the latest Opus 5 scores; Haiku 4.5
   on the same set if Willie wants the cheaper arm. Record cost per memo for each.
2. **Runtime, side by side.** Deploy `-app`. Parity with the local Bedrock run: `gold.py --free`
   77/77 identical; the paid gold set scores the same within noise. Measure cold and warm latency,
   and the bill at rest after 7 idle days.
3. **Identity + Observability + Gateway + Policy.** Alpha Vantage key in the credential provider;
   caller auth on the Runtime endpoint; a call from `deploy/call.py`. Compare a trace in
   CloudWatch with the run record. Deploy `-tools`: the Strands chat tools and the memo as one MCP
   tool behind Gateway; Cedar policies; a red-team set (~20 bad calls) must all be blocked and the
   normal set must all pass.
4. **Evaluations.** Deploy `-evals`: gold checks as code-based evaluators; a built-in faithfulness
   evaluator and a Haiku 4.5 judge on the hand-labelled claim-check cases (do they agree with
   8/8?); online evaluation on a sample.
5. **Write-up and decision** (below), then teardown of whatever the decision doesn't keep.

## Comparison (fixed before any build)

| Measure | How |
|---|---|
| Memo quality (model change) | Paid gold set: Opus 4.5 on Bedrock vs the latest Opus 5 scores; Haiku 4.5 optional |
| Parity (platform change) | `gold.py --free` 77/77 identical; paid gold scores equal within noise, local Bedrock vs Runtime |
| Latency | p50/p95 of submit → result, cold and warm, 10 memos per side (paid, ~$0.50–1, ask first) |
| Cost at rest | AWS bill for each stack after 7 idle days (Cost Explorer, by tag) |
| Cost per memo | Bedrock tokens (by inference profile) + platform (Runtime vCPU/GB time, Gateway, Policy, Evaluations) |
| Moving parts | Lines of infra and glue code removed vs added; number of AWS resources; untagged resources (must be 0 except `GatewayTarget`/`Policy`) |
| Safety | Red-team calls blocked by Policy; claim-check agreement of built-in and Haiku evaluators |

**Decision rule.** Move the service (and its caller) to AgentCore on Bedrock only if all hold:
1. Opus 4.5's gold score is no worse than Opus 5's by more than one check (otherwise the model,
   not the platform, blocks the move: stay on the Anthropic API, and the write-up says so);
2. platform parity holds (free 77/77 identical, paid scores equal within noise);
3. cost at rest is under $1/month and cost per memo no more than 20% above today's;
4. p95 latency is no worse than Lambda's;
5. the glue code AgentCore replaces is larger than the glue it adds.

Otherwise the Lambda stack stays and the AgentCore stacks are deleted; the write-up says which
pieces were worth it on their own (Evaluations or Gateway can be adopted without Runtime).

## Cost (estimate; check current prices before Phase 1)

- **Model:** Opus 4.5 on Bedrock is $5 / $25 per million input/output tokens (global profile;
  `us.` profiles cost 10% more), from the Price List API via robinhood-agent's `config/prices.yaml`.
  Today's memos cost $0.03–0.09 on Opus 5; Phase 1 measures the Bedrock figure. Haiku 4.5 is
  $1 / $5.
- **Runtime:** published at about $0.0895 per vCPU-hour and $0.00945 per GB-hour (us-east-1),
  billed on active CPU and memory; a memo is mostly waiting on the model. An upper bound of 1 vCPU
  and 2 GB for 60 s is under $0.002 per memo. Idle sessions may keep billing memory until their
  idle timeout, so the template sets a short one; confirm on the bill.
- **Gateway, Policy, Evaluations:** billed per call or per evaluation; no reliable published
  figures found yet. Phase 0 looks them up.
- **Whole experiment:** under $5 including paid gold runs, behind the $5 budget; teardown at the
  end unless the decision rule says move.

## Risks and unknowns

- **Model access:** only Opus 4.5 and Haiku 4.5 are available to this account on Bedrock. If Opus
  4.5 scores clearly worse than Opus 5, the model blocks the move regardless of the platform.
- Opus 4.5 feature differences from Opus 5 (thinking, effort, strict tool schemas, refusals).
- Policy's GA status (AWS's own pages disagree) and its exact limits on tool arguments.
- `GatewayTarget` and `Policy` can't be tagged; CloudBurn's scan may not cover AgentCore types.
- Lock-in: Runtime's entrypoint and Identity's auth are AgentCore-specific. Keep them in one
  adapter module so the graph stays portable.

## Sources

- AWS News Blog, *AgentCore adds quality evaluations and policy controls*:
  https://aws.amazon.com/blogs/aws/amazon-bedrock-agentcore-adds-quality-evaluations-and-policy-controls-for-deploying-trusted-ai-agents/
- AgentCore Evaluations GA (2026-03): https://aws.amazon.com/about-aws/whats-new/2026/03/agentcore-evaluations-generally-available
- Evaluators (built-in, custom, model choice): https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluators.html
- AgentCore CLI (frameworks, providers): https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-get-started-cli.html
- Pricing: https://aws.amazon.com/bedrock/agentcore/pricing/ (third-party breakdown:
  https://cloudburn.io/blog/amazon-bedrock-agentcore-pricing)
- Model availability and CloudFormation types: AWS CLI on this account, 2026-10-29
  (`bedrock get-foundation-model-availability`, `cloudformation list-types` / `describe-type`).

## Phase 0 results (2026-10-29, $0)

Branch `agentcore`. 352 tests pass, none of them touching AWS or a model.

**Decoupling, provider switch.** `provider: anthropic | bedrock` in `config.yaml` (or
`FIN_ANALYST_PROVIDER`); the default stays `anthropic`, so the Lambda stack is unchanged.
- `llm.py`: `make_client` builds `anthropic.AnthropicBedrock` (AWS credentials, no key) or
  `anthropic.Anthropic`. `_request` gives Bedrock its own shape: the model becomes the tagged
  profile ARN (`FIN_ANALYST_PROFILE_*`) or the global profile; no thinking; effort through its beta
  only where the model has it (Opus 4.5, not Haiku 4.5); no refusal fallbacks (a Claude API
  feature); strict tools unless the model entry says otherwise. Errors name the AWS cause (no
  model access, no credentials) instead of an API key.
- `strands_chat.py`: the chat agent gets a Strands `BedrockModel` (Converse API, effort in
  `additionalModelRequestFields`) on Bedrock, `AnthropicModel` otherwise.
- Cost: `claude-opus-4-5` priced at $5 / $25 per million (global profile).
- Local Bedrock runs with `aws login` credentials need botocore's CRT extra (`awscrt` added to
  the dev requirements); the Runtime uses its role and doesn't.

**Feature differences, from documentation (to confirm with the probe):** Opus 4.5 on Bedrock has
no adaptive thinking; manual thinking can't be combined with a forced tool (the plan and verify
steps force one); effort is a beta (`effort-2025-11-24`); structured outputs are listed for Opus
4.5 on Bedrock, strict tools not explicitly. `scripts/bedrock_probe.py` checks each with tiny real
calls: 2 models x 7 calls, at most $0.09. **Not run: needs Willie's OK.**

**Runtime entry point.** `fin_analyst/agentcore_app.py`: `/ping` (`HealthyBusy` while a memo runs)
and `/invocations`, which runs the memo in the request and returns the finished job. It reuses the
Lambda stack's worker, S3 job ledger, caches and daily caps, so what Runtime replaces is exactly
the self-invoke and the polling. Refuses to start unless the provider is Bedrock.
`Dockerfile.agentcore` builds it with no secrets; an amd64 build ran locally (`/ping` healthy,
bad requests refused). This machine has no arm64 emulation, so the real image is built by
CodeBuild on ARM.

**CloudFormation.** All three templates pass `cfn-lint` and CloudFormation's `validate-template`;
CloudBurn reports only the medium storage-class note the Lambda stack already accepts (objects of
a few KB). Every taggable resource carries the required tags (tested); `deploy/agentcore/check_tags.py`
checks the deployed resources after `02_app.sh --execute`. AgentCore permissions for the deployer
are account-wide in the region (`bedrock-agentcore:*`): resource names can't be prefix-scoped.

**Prices (AWS Price List API, us-east-1, 2026-10-29):** Runtime $0.0895 per vCPU-hour and $0.00945
per GB-hour (a "v2" tier: $0.1276 and $0.0169); Gateway $0.005 per 1,000 calls; Evaluations
built-in judges $2.40 / $12 per million tokens, custom evaluators $0.0015 each; Policy not listed.
At the v2 rates, 1 vCPU and 2 GB for a 60 s memo is under $0.003 of platform cost.

**Next (Phase 1, paid, ask first):** run the probe (≤ $0.09); deploy `-iam` as admin (free; the
budget and profiles cost nothing at rest); paid gold set on Opus 4.5 locally vs the latest Opus 5
scores.

## Phase 1 results (2026-10-29)

- **Probe** (`runs/bedrock_probe_20261029-121302.json`, $0.012): every `bedrock.models` claim holds.
  Opus 4.5: effort OK, strict forced tool OK, Converse OK. Haiku 4.5: effort rejected ("does not
  support the effort parameter"), the rest OK. Both: thinking with a forced tool rejected, adaptive
  thinking rejected. Thinking stays off on Bedrock.
- **`fin-analyst-agentcore-iam` deployed** (by Willie, as admin): 6 resources, 0 tag problems
  (`check_tags.py`); Opus 4.5 profile ACTIVE; budget $5 on `user:component$agentcore`
  (`component` was already an active cost allocation tag).
- **Gold set, same code, same filings, same day** (12 questions, 43 checks):

  | | Score | Cost | Memos published | First-draft passes | Content checks missed |
  |---|---|---|---|---|---|
  | Opus 5, Claude API (`runs/gold-20261029T172343Z`) | **38/43** | $0.78 | 12/12 | 8/12 | JPM liquidity didn't say the ratio doesn't apply |
  | Opus 4.5, Bedrock (`runs/gold-20261029T171406Z`) | **36/43** | $0.51 | 11/12 | 8/12 | MSFT interest coverage: no memo after 3 drafts ("comfortably"); COST leverage: 3 years, not 4; AAPL interest: didn't say unavailable |

  **Decision rule 1 fails:** Opus 4.5 is 2 checks behind (the rule allows 1). Retries were equal
  (4 each); the gap is in content: 3 misses vs 1, and one memo not published. Opus 4.5 cost 35%
  less. One run each, so the gap is within noise, but the rule was fixed in advance and is applied
  as written: **the model, not the platform, blocks the move. No Lambda teardown.**

### Retry arm, registered before running (2026-10-29)

One pre-registered retry, Willie's choice (option 1). **Change:** on Bedrock, the `write` node
gets manual thinking (`thinking_budget.write: 2048`, its ceiling raised to 1500 + 2048); plan and
verify unchanged (a forced tool can't think). Everything else as the 36/43 run: same code
otherwise, same 12 questions, same filings (cached), Opus 4.5 through the global profile.
**Pass bar, fixed now:** at least **37/43** checks (within one of Opus 5's 38) **and all 12 memos
published**. Cost cap $1.00 (`--max-usd`). Pass → decision rule 1 holds and Phase 2 starts; fail →
the model blocks the move, as recorded above, and no further model retries are run.

**Retry result: FAIL** (`runs/gold-20261029T173945Z`, $0.64): **34/43**, 11/12 memos published.
Thinking fixed the MSFT interest-coverage memo (first draft, no "comfortably"), but a new failure
appeared: the JPM efficiency memo gave up after 3 drafts because the claim check kept catching a
claim that inverted the direction of a change in the cited passage (the guardrail worked; the
memo didn't). Rewrites rose from 4 to 8; COST still 3 years. Across the two Opus 4.5 runs the score
moved 36 → 34, so one run varies by about ±2 checks; Opus 5's 38 is also one run.
**As registered: the model blocks the move. No further model retries, no Lambda teardown.** The
config keeps the default of no thinking on Bedrock (the better of the two Opus 4.5 arms).

## Revised plan: AgentCore with the Claude API (decided 2026-10-29)

Opus 5 has no announced date on Bedrock, and the model blocks the Bedrock move. Willie chose to
keep testing the platform with the model memos use today: **AgentCore Runtime calling Opus 5
through the Claude API** (`provider: anthropic` on the Runtime). The Bedrock path stays in the code
and the two tagged profiles stay deployed, for when Opus 5 reaches Bedrock or for a Haiku 4.5
judge in Phase 4.

What this gives up: Claude spend stays on the Anthropic bill (not tagged in Cost Explorer), and
there is still an API key to fund and rotate.

**The key, through AgentCore Identity (pulled forward from Phase 3).** A Secrets Manager secret
(CloudFormation, tagged; value written by `deploy/agentcore/03_secret.sh` from `.env` on stdin,
never printed) behind an `ApiKeyCredentialProvider` (`EXTERNAL` source). Runtime hands the
container a workload access token per request, which needs the caller to name a user
(`runtimeUserId` on InvokeAgentRuntime); the container exchanges it with `GetResourceApiKey`. No key
in the image, the environment or a CloudFormation parameter. Cost: $0.40/month for the secret.

**Decision rule, revised.** Condition 1 (model quality) no longer applies: both stacks run the
same model. Platform parity: `gold.py --free` identical on both, and the paid gold set through the
Runtime within **2 checks** of the Lambda's (one run varies by about ±2, measured above). Conditions
3-5 (cost at rest < $1/month, p95 latency no worse, less glue code) stand.

**If it passes: Lambda teardown plan.** AuraTrade moves from the function URL (SigV4) to
`InvokeAgentRuntime` with `runtimeUserId` (a change in its private repo, with its own test), one
week of both stacks serving, then `deploy/99_teardown.sh` for the Lambda stacks and SSM secrets.

## Phase 2 results: Runtime with the Claude API (2026-10-29)

Deployed: `fin-analyst-agentcore-build` (arm64 image `27f85e2` built by CodeBuild on ARM) and
`fin-analyst-agentcore-app` (Runtime READY, endpoint `live`, jobs bucket, secret + Identity
credential provider); 15 resources, 0 tag problems. The key reached the container only through
AgentCore Identity (`runtimeUserId` → workload token → `GetResourceApiKey`).

- **First memo** (MSFT liquidity): $0.052, 41.5 s end to end including a cold start; same shape
  and rules as the Lambda's memos.
- **Gold set through the Runtime** (`gold.py --spend --runtime`, `runs/gold-runtime-20261029T183545Z`):
  **41/43**, $0.65, all 12 published, 11/12 first drafts. Misses: JPM liquidity didn't say the ratio
  doesn't apply (the same miss Opus 5 had locally), TRV needed 3 drafts.
  **Parity: PASS** (bar 36; the local Opus 5 run scored 38, so within noise either way).
- **Latency, submit → finished memo:** p50 20.4 s, max 52.8 s (TRV, 3 drafts).

Still open before the decision: Lambda latency measured the same way (the 10-23 memo took ~17 s,
one sample), cost at rest after 7 idle days (from 2026-10-29), and the glue-code count.

**Lambda, same method** (`gold.py --spend --lambda`, `runs/gold-lambda-20261029T184702Z`): 39/43,
$0.75, p50 18.5 s, max 50.3 s. (A first attempt died on a dropped HTTPS poll after 2 memos, $0.13;
polls now retry, submissions never do.)

**Latency condition as registered: FAIL** — Runtime max 52.8 s vs Lambda 50.3 s (p95 by nearest
rank with 12 memos is the max). Per question with the same draft count (n=10), Runtime − Lambda,
median: end to end +1.8 s; inside the graph −0.8 s (the arm64 container does the work slightly
faster); Claude time −0.4 s; **outside the graph +2.7 s**. The max is set by TRV, 3 drafts on both
sides, where the Claude API itself took 41.5 s vs 33.6 s.

### Latency amendment (registered 2026-10-29, after seeing the result above, before re-measuring)

Openly an amendment, made because the registered metric measured the wrong thing: with 12 memos,
p95 is one memo, and its value is set by Claude API variance (8 s on the same question), not by the
platform. The cause of the real platform gap is also known: every memo opened a new Runtime
session (a fresh microVM, and a fresh key fetch from Identity), while the Lambda reuses warm
containers.

- **Fix first:** a caller reuses one Runtime session for its memos (`evals/gold.py --runtime` and
  `deploy/agentcore/invoke.py` per run; AuraTrade per screen), so the container and key stay warm
  within the 300 s idle timeout. Client-side only; no redeploy.
- **Metric:** platform overhead = end-to-end seconds as the caller sees them − the graph's own
  span (last trace event). Lambda polls every 0.25 s so polling adds at most a quarter second.
- **Sample, fixed now:** the first 6 gold questions (MSFT x4, COST x2) on each side. Runtime now;
  Lambda after 00:00 UTC, when its $1/day per-key cap resets (today: $0.88 used). Cost ≈ $0.35 each.
- **Pass bar:** median Runtime overhead ≤ median Lambda overhead + 1.0 s. The registered p95 result
  above stays on the record as a FAIL either way.
