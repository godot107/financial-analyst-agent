"""Calling the memo service on AgentCore Runtime (docs/AGENTCORE_PLAN.md). SPENDS MONEY.

A caller signs InvokeAgentRuntime with its AWS credentials and names a user
(`runtimeUserId`): that makes Runtime hand the container a workload access token,
which AgentCore Identity exchanges for the Claude API key. Used by
deploy/agentcore/invoke.py and `evals/gold.py --runtime`; AuraTrade would call it
the same way.
"""

import json
import uuid
from types import SimpleNamespace

STACK = "fin-analyst-agentcore-app"


def runtime_target(cloudformation=None) -> tuple[str, str]:
    """(runtime ARN, endpoint name) from the app stack's outputs."""
    import boto3

    cloudformation = cloudformation or boto3.client("cloudformation")
    outputs = cloudformation.describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]
    out = {o["OutputKey"]: o["OutputValue"] for o in outputs}
    return out["RuntimeArn"], out["EndpointName"]


def new_session(caller: str) -> str:
    """A Runtime session id (33+ characters). Reuse one for a caller's run of memos: the
    session keeps its container, and the container keeps its key, until the idle timeout
    (300 s). A new session per memo costs a fresh microVM and key fetch, ~2.7 s each."""
    return f"{caller}-{uuid.uuid4()}"


def invoke_memo(request: dict, caller: str, client=None, target=None, session: str | None = None) -> dict:
    """One memo, run to completion on the Runtime. Returns the finished job."""
    import boto3

    client = client or boto3.client("bedrock-agentcore")
    arn, endpoint = target or runtime_target()
    response = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        qualifier=endpoint,
        runtimeSessionId=session or new_session(caller),
        runtimeUserId=caller,
        contentType="application/json",
        accept="application/json",
        payload=json.dumps({**request, "caller": caller}).encode(),
    )
    return json.loads(response["response"].read())


def as_state(job: dict) -> SimpleNamespace:
    """A finished job, shaped like the graph's state for evals/gold.py's grader."""
    result = job.get("result") or {}
    metrics = [SimpleNamespace(fiscal_year=m["fiscal_year"], value=m["value"]) for m in result.get("metrics", [])]
    return SimpleNamespace(
        memo=job.get("memo"),
        error=job.get("error"),
        metric_ids=result.get("metric_ids", []),
        metrics=metrics,
        # The grader counts drafts; the job reports how many there were.
        drafts=[None] * int(result.get("drafts") or 0),
    )


def graph_seconds(job: dict) -> float | None:
    """How long the graph itself ran (its trace's last event), whichever service ran it."""
    trace = (job.get("result") or {}).get("trace") or []
    return trace[-1]["seconds"] if trace else None
