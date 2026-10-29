# Plan: the same analyst on Amazon Bedrock AgentCore, measured against the Lambda stack

Status: **planned, not built.** Nothing here has been run, deployed or spent. Drafted 2026-10-29.

## The question

The memo service already runs on AWS, but every piece of the platform around the agent is
hand-built: a Lambda that invokes itself for async jobs, an S3 job ledger, daily spend caps in
the API, secrets in SSM, an API key plus SigV4 for callers, `trace.py` for the run record, and
`evals/gold.py` for quality. AgentCore sells most of those as managed services.

> Which of the hand-built pieces does AgentCore actually replace, what does each one cost in
> money, latency and lock-in, and does the memo come out exactly the same?

The answer is a write-up either way: "moved, here's what got simpler" or "kept Lambda, here's
why", with the numbers. It's also the reference design for a pattern used elsewhere in this
workspace: **an LLM proposes, a checker critiques, code has the final word, and every step is
recorded.**

## What must not change

- **The graph.** plan → fetch → compute → retrieve → write → check → verify → render stays a fixed
  LangGraph pipeline. AgentCore hosts it; it doesn't redesign it.
- **Claude never writes a number.** Placeholders, the leak check and the claim check stay in the
  graph, in Python, exactly as today. Nothing in AgentCore replaces them.
- **The Anthropic API, called directly** (`llm.py`). Claude on Bedrock isn't an option on this
  AWS account, and the memo's model, effort and token settings are tuned for the direct API.
- **The run record** (`service.structured_result` + `trace.py`) stays the contract. AgentCore
  Observability is extra, not a replacement: programs read the run record, not CloudWatch.
- **Tests stay offline** (`tests/conftest.py` blocks sockets). AgentCore code is wrapped so the
  graph imports nothing from it; the entrypoint is a thin adapter like `lambda_handler.py`.
- **The Lambda stack keeps running** until the decision at the end. The existing caller stays on
  it; nothing switches over mid-experiment.
- **House rules:** ask before any paid run, with the estimated cost; the budget guard stays on.

## AgentCore pieces, one decision each

| Today (hand-built) | AgentCore piece | Decision | Why |
|---|---|---|---|
| Lambda + function URL + self-invoke for async jobs; 300 s limit | **Runtime** (hosts any framework; long-running async sessions) | **Adopt and measure** | The main test. Could remove the self-invoke trick and the 300 s ceiling. Compare cold start, latency, cost at rest |
| X-API-Key + SigV4 on the function URL; keys in SSM | **Identity** (inbound auth for callers; outbound credential store for API keys) | **Adopt** | Anthropic and Alpha Vantage keys move to the credential store; caller auth becomes IAM/OAuth on the Runtime endpoint. Compare with today's key handling |
| `trace.py` → run record + CloudWatch JSON lines | **Observability** (OpenTelemetry traces, CloudWatch dashboards) | **Adopt alongside** | Free-ish extra view of each node and call. The run record stays the source of truth |
| `evals/gold.py` (77 free checks + paid set), claim check graded 8/8 by hand | **Evaluations** (built-in evaluators, custom code-based evaluators, custom LLM judges, online sampling) | **Adopt for monitoring** | Gold checks become **code-based evaluators** (no model needed). Built-in evaluators run on a sample of live memos; compare their faithfulness scores with our claim check's hand labels. Custom LLM judges must be Bedrock models, so not Claude on this account: gpt-oss or Nova only |
| Tools are Python functions inside the graph; the Strands chat (`--chat-mode strands`) calls them as tools | **Gateway** (tools served over MCP, with search) | **Adopt for the chat agent and for memo-as-a-tool only** | The fixed pipeline gains nothing from calling its own functions over the network. Two real uses: (1) the Strands chat agent's tools behind Gateway; (2) the memo service itself exposed as one MCP tool other agents can call |
| Pydantic limits, request caps (`max_usd`, filings ≤ 5), daily spend caps in the API | **Policy** (Cedar rules checked on every Gateway tool call, outside the agent) | **Test** | A second fence, not a replacement: e.g. ticker format, `filings ≤ 5`, `max_usd ≤ cap`, market tool only when asked. Measured with a red-team set of bad tool calls. Daily spend caps need state, so they stay in code. Confirm Policy's GA status first (AWS's pages disagree) |
| Memos cached by hash; chat state in memory | **Memory** | **Skip** | Memos are stateless by design; long-term memory would make runs less reproducible |
| — | Code Interpreter, Browser | **Skip** | Numbers come from pre-written Python, never generated code; no web browsing |

## Phases

Each phase ends with a commit, a short results note in this file, and a Trello comment.

0. **Offline spike ($0).** AgentCore CLI/starter toolkit locally: wrap `run_memo` in the Runtime
   entrypoint, run it in the local Runtime container against fixtures with a fake Claude. Confirm
   the direct Anthropic API path works from Runtime (AWS samples use Bedrock; ours is unverified),
   whether AgentCore resources can be created by CloudFormation (our deploy is CFN + numbered
   scripts) or need the CLI, and how resources get tagged (`shared/TAGGING.md`). Look up current
   prices on the AWS pricing page.
1. **Runtime, side by side.** Deploy the same image to Runtime next to the Lambda stack. Parity:
   `gold.py --free` 77/77 identical; the same request yields the same memo hash (cache off). One
   paid memo (~$0.05, ask first). Measure cold and warm latency, and the bill at rest after a week
   of idling.
2. **Identity + Observability.** Credentials out of SSM into the credential store; caller auth on
   the Runtime endpoint; a call from `deploy/call.py`. Compare a trace in CloudWatch with the run
   record for the same memo.
3. **Gateway + Policy.** The Strands chat agent's tools behind Gateway; the memo as one MCP tool.
   Cedar policies for the limits above; a red-team set (~20 bad calls: wrong tickers, oversized
   requests, injected tool arguments) must all be blocked, and the normal set must all pass.
4. **Evaluations.** Gold checks as code-based evaluators; a built-in faithfulness evaluator on
   the hand-labelled claim-check cases (does it agree with our 8/8?); online evaluation on a sample.
5. **Write-up and decision** (below).

## Comparison (fixed before any build)

| Measure | How |
|---|---|
| Parity | `gold.py --free` 77/77 identical; same memo hash for the same request and code |
| Latency | p50/p95 of submit → result, cold and warm, 10 memos each side (paid, ~$0.50, ask first) |
| Cost at rest | AWS bill for each stack after 7 idle days (Cost Explorer, by tag) |
| Cost per memo | Claude (unchanged) + platform (Runtime vCPU/GB time, Gateway, Policy, Evaluations) |
| Moving parts | Lines of infra and glue code removed vs added; number of AWS resources |
| Safety | Red-team calls blocked by Policy; claim-check agreement of built-in evaluators |

**Decision rule.** Move the service (and its caller) to AgentCore only if parity holds exactly,
cost at rest stays under $1/month, p95 latency is no worse than Lambda's, and the glue code it
replaces is larger than the glue it adds. Otherwise the Lambda stack stays and AgentCore is torn
down; the write-up says which pieces were worth it on their own (Evaluations or Gateway can be
adopted without Runtime).

## Cost (estimate; check current prices before Phase 1)

- **Claude:** unchanged, $0.03–0.09 per memo.
- **Runtime:** published at about $0.0895 per vCPU-hour and $0.00945 per GB-hour (us-east-1),
  billed on active CPU and memory; a memo is mostly waiting on Claude. An upper bound of 1 vCPU and
  2 GB for 60 s is under $0.002 per memo. Idle sessions may keep billing memory until their idle
  timeout, so set a short one and confirm on the bill.
- **Gateway, Policy, Evaluations:** billed per call or per evaluation; no reliable published
  figures found yet. Phase 0 looks them up.
- **Whole experiment:** under $5 including paid memos, behind a separate AWS Budget at $5 with
  alerts; teardown at the end unless the decision rule says move.

## Risks and unknowns

- Direct Anthropic API from Runtime: supported by the CLI's provider list, but no sample uses it.
- Policy's GA status (AWS's own pages disagree) and its exact limits on tool arguments.
- Custom LLM judges are Bedrock-only; on this account that means no Claude judge.
- CloudFormation support and tagging for every AgentCore resource; CloudBurn's scan may not cover
  them.
- Lock-in: Runtime's entrypoint and Identity's auth are AgentCore-specific. Keep them in one
  adapter module so the graph stays portable.

## Sources

- AWS News Blog, *AgentCore adds quality evaluations and policy controls*:
  https://aws.amazon.com/blogs/aws/amazon-bedrock-agentcore-adds-quality-evaluations-and-policy-controls-for-deploying-trusted-ai-agents/
- AgentCore Evaluations GA (2026-03): https://aws.amazon.com/about-aws/whats-new/2026/03/agentcore-evaluations-generally-available
- Evaluators (built-in, custom, model choice): https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluators.html
- AgentCore CLI (frameworks, non-Bedrock providers): https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-get-started-cli.html
- Pricing: https://aws.amazon.com/bedrock/agentcore/pricing/ (third-party breakdown:
  https://cloudburn.io/blog/amazon-bedrock-agentcore-pricing)
