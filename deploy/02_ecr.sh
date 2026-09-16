#!/usr/bin/env bash
# The image repository, as the deployer.
source "$(dirname "$0")/lib.sh"

scan
say "Deploying '$ECR_STACK'"
aws cloudformation deploy \
  --stack-name "$ECR_STACK" \
  --template-file "$ROOT/infra/ecr.yaml" \
  --no-fail-on-empty-changeset \
  --parameter-overrides ProjectName="$PROJECT"
echo "  $(stack_output "$ECR_STACK" RepositoryUri)"
echo
echo "  Next: ./deploy/03_image.sh"
