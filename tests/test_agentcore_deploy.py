"""The AgentCore deploy helpers' logic, without AWS."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy" / "agentcore" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FULL = {"project": "fin-analyst", "environment": "dev", "owner": "williemaize",
        "cost-center": "personal", "managed-by": "cloudformation", "component": "agentcore"}


def resource(logical, kind, physical):
    return {"LogicalResourceId": logical, "ResourceType": kind, "PhysicalResourceId": physical}


def test_a_fully_tagged_stack_has_no_problems():
    check = load("check_tags")
    resources = [resource("JobsBucket", "AWS::S3::Bucket", "fin-analyst-agentcore-jobs-1-us-east-1"),
                 resource("RuntimeRole", "AWS::IAM::Role", "fin-analyst-agentcore-runtime")]
    tags = {"arn:aws:s3:::fin-analyst-agentcore-jobs-1-us-east-1": FULL}
    assert check.problems(resources, tags, {"fin-analyst-agentcore-runtime": FULL}) == []


def test_missing_and_partial_tags_are_reported_and_untaggable_types_skipped():
    check = load("check_tags")
    resources = [
        resource("Runtime", "AWS::BedrockAgentCore::Runtime", "fin_analyst_memo-AbC123"),
        resource("Bucket", "AWS::S3::Bucket", "b"),
        resource("Target", "AWS::BedrockAgentCore::GatewayTarget", "t"),
    ]
    partial = {k: v for k, v in FULL.items() if k != "owner"}
    tags = {"arn:aws:bedrock-agentcore:us-east-1:1:runtime/fin_analyst_memo-AbC123": partial}
    found = check.problems(resources, tags, {})
    assert found == ["Runtime (AWS::BedrockAgentCore::Runtime): missing owner",
                     "Bucket (AWS::S3::Bucket): no tags found"]


def test_the_templates_tag_every_taggable_resource():
    """Every resource with a Tags property in the templates carries the required keys."""
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    check = load("check_tags")
    for name in ("iam", "build", "app"):
        template = yaml.load((ROOT / "infra" / "agentcore" / f"{name}.yaml").read_text(), Loader=Loader)
        for logical, res in template["Resources"].items():
            tags = (res.get("Properties") or {}).get("Tags")
            if tags is None:
                continue
            keys = set(tags) if isinstance(tags, dict) else {t["Key"] for t in tags}
            assert check.REQUIRED <= keys, f"{name}.yaml {logical} lacks {check.REQUIRED - keys}"
            assert "component" in keys
