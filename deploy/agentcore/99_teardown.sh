#!/usr/bin/env bash
# Deletes the AgentCore stacks: app and build as the deployer; with --iam, the
# bootstrap too, as admin. The Lambda stack (fin-analyst-app) is never touched.
source "$(dirname "$0")/common.sh"
for STACK in "$AC_APP_STACK" "$AC_BUILD_STACK"; do
  say "Deleting $STACK"
  aws cloudformation delete-stack --stack-name "$STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK"
done
if [ "${1:-}" = "--iam" ]; then
  say "Deleting $AC_IAM_STACK as $ADMIN_PROFILE"
  AWS_PROFILE="$ADMIN_PROFILE" aws cloudformation delete-stack --stack-name "$AC_IAM_STACK"
  AWS_PROFILE="$ADMIN_PROFILE" aws cloudformation wait stack-delete-complete --stack-name "$AC_IAM_STACK"
fi
echo "Runtime log groups (/aws/bedrock-agentcore/runtimes/...) are created by AgentCore, not CloudFormation: delete them by hand."
