#!/usr/bin/env bash
# AgentCore bootstrap, as ADMIN: the tagged Bedrock inference profiles, the runtime
# and build roles, the deployer's extra permissions, and the $5 budget.
# Run once (again only when infra/agentcore/iam.yaml changes). Needs the fin-analyst-iam stack.
source "$(dirname "$0")/common.sh"
scan
say "Deploying $AC_IAM_STACK as $ADMIN_PROFILE"
AWS_PROFILE="$ADMIN_PROFILE" aws cloudformation deploy \
  --stack-name "$AC_IAM_STACK" --template-file "$AC_DIR/iam.yaml" \
  --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset \
  --parameter-overrides ProjectName="$PROJECT" BudgetEmail="$BUDGET_EMAIL" BudgetLimitUsd="$BUDGET_USD" \
  --tags "${TAGS[@]}"
AWS_PROFILE="$ADMIN_PROFILE" aws cloudformation describe-stacks --stack-name "$AC_IAM_STACK" \
  --query "Stacks[0].Outputs" --output table
echo "Activate 'component' as a cost allocation tag in Billing, or the budget filter matches nothing."
