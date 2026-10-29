"""After a deploy: does every AgentCore-stack resource that can carry tags carry the required ones?

shared/TAGGING.md requires project, environment, owner, cost-center and managed-by. Stack-level
--tags reach most resources, but not every type takes them (AgentCore's GatewayTarget and Policy
can't be tagged at all), and a missed one is invisible in Cost Explorer. Read-only; free.

    .venv/bin/python deploy/agentcore/check_tags.py
"""

import re
import sys

REQUIRED = {"project", "environment", "owner", "cost-center", "managed-by"}
STACKS = ("fin-analyst-agentcore-iam", "fin-analyst-agentcore-build", "fin-analyst-agentcore-app")
# Types that can't carry tags, or whose tags this check can't read through the tagging API.
UNTAGGABLE = {
    "AWS::BedrockAgentCore::GatewayTarget",
    "AWS::BedrockAgentCore::Policy",
    "AWS::IAM::ManagedPolicy",
    "AWS::Budgets::Budget",
}
IAM_ROLE = "AWS::IAM::Role"


def owns(arn: str, physical: str) -> bool:
    """Is `physical` a whole segment of `arn`? ("b" is not part of "...:runtime/fin_analyst_memo-AbC")"""
    return arn == physical or re.search(rf"[:/]{re.escape(physical)}(?:$|[:/])", arn) is not None


def problems(resources: list[dict], tags_by_arn: dict[str, dict], role_tags: dict[str, dict]) -> list[str]:
    """Each stack resource that should be tagged and isn't, with what it's missing.

    `resources`: CloudFormation stack resources (LogicalResourceId, PhysicalResourceId, ResourceType).
    `tags_by_arn`: from the Resource Groups Tagging API. `role_tags`: role name -> tags (IAM).
    A resource is matched to an ARN by its physical id, which every ARN here contains as a
    whole segment (after a ':' or '/', and ending the ARN or followed by one).
    """
    found = []
    for resource in resources:
        kind, physical = resource["ResourceType"], resource.get("PhysicalResourceId") or ""
        if kind in UNTAGGABLE:
            continue
        if kind == IAM_ROLE:
            tags = role_tags.get(physical)
        else:
            tags = next((t for arn, t in tags_by_arn.items() if physical and owns(arn, physical)), None)
        if tags is None:
            found.append(f"{resource['LogicalResourceId']} ({kind}): no tags found")
        elif missing := REQUIRED - set(tags):
            found.append(f"{resource['LogicalResourceId']} ({kind}): missing {', '.join(sorted(missing))}")
    return found


def main() -> int:
    import boto3
    from botocore.exceptions import ClientError

    cfn, tagging, iam = boto3.client("cloudformation"), boto3.client("resourcegroupstaggingapi"), boto3.client("iam")
    resources = []
    for stack in STACKS:
        try:
            pages = cfn.get_paginator("list_stack_resources").paginate(StackName=stack)
            resources += [r for page in pages for r in page["StackResourceSummaries"]]
        except ClientError:
            print(f"{stack}: not deployed, skipped")

    tags_by_arn = {}
    for page in tagging.get_paginator("get_resources").paginate(
        TagFilters=[{"Key": "component", "Values": ["agentcore"]}]
    ):
        for item in page["ResourceTagMappingList"]:
            tags_by_arn[item["ResourceARN"]] = {t["Key"]: t["Value"] for t in item["Tags"]}
    role_tags = {
        r["PhysicalResourceId"]: {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=r["PhysicalResourceId"])["Tags"]}
        for r in resources
        if r["ResourceType"] == IAM_ROLE
    }

    found = problems(resources, tags_by_arn, role_tags)
    for line in found:
        print("UNTAGGED", line)
    print(f"{len(resources)} resources checked, {len(found)} problems")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
