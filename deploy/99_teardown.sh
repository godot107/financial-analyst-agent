#!/usr/bin/env bash
# Removes everything the deploy created, then checks nothing billable is left.
#   ./deploy/99_teardown.sh          service, repository, secrets (as the deployer)
#   ./deploy/99_teardown.sh --iam    also the deployer, boundary and budget (as admin)
source "$(dirname "$0")/lib.sh"

BUCKET="$(stack_output "$APP_STACK" BucketName 2>/dev/null || true)"
if [ -n "$BUCKET" ] && [ "$BUCKET" != "None" ]; then
  say "Emptying s3://$BUCKET (CloudFormation cannot delete a bucket with objects in it)"
  aws s3 rm "s3://$BUCKET" --recursive --only-show-errors
fi

for STACK in "$APP_STACK" "$ECR_STACK"; do
  if aws cloudformation describe-stacks --stack-name "$STACK" >/dev/null 2>&1; then
    say "Deleting '$STACK'"
    aws cloudformation delete-stack --stack-name "$STACK"
    aws cloudformation wait stack-delete-complete --stack-name "$STACK"
  fi
done

say "Deleting secrets under /$PROJECT/"
NAMES="$(aws ssm get-parameters-by-path --path "/$PROJECT" --query 'Parameters[].Name' --output text 2>/dev/null || true)"
[ -z "$NAMES" ] || aws ssm delete-parameters --names $NAMES >/dev/null
rm -f "$HERE/.image"

if [ "${1:-}" = "--iam" ]; then
  admin() { aws --profile "$ADMIN_PROFILE" "$@"; }
  USER_NAME="$PROJECT-deployer"
  say "Deleting $USER_NAME's access keys (as admin)"
  for KEY in $(admin iam list-access-keys --user-name "$USER_NAME" --query 'AccessKeyMetadata[].AccessKeyId' --output text 2>/dev/null); do
    admin iam delete-access-key --user-name "$USER_NAME" --access-key-id "$KEY"
  done
  say "Deleting '$IAM_STACK' (deployer, boundary, budget)"
  admin cloudformation delete-stack --stack-name "$IAM_STACK"
  admin cloudformation wait stack-delete-complete --stack-name "$IAM_STACK"
fi

say "Checking nothing is left"
LEFT=0
for STACK in "$APP_STACK" "$ECR_STACK"; do
  if aws cloudformation describe-stacks --stack-name "$STACK" >/dev/null 2>&1; then echo "  still there: $STACK"; LEFT=1; fi
done
if [ -n "$(aws ssm get-parameters-by-path --path "/$PROJECT" --query 'Parameters[].Name' --output text 2>/dev/null || true)" ]; then
  echo "  secrets still under /$PROJECT"; LEFT=1
fi
[ "$LEFT" -eq 0 ] && echo "  nothing billable left from $PROJECT" || die "teardown incomplete"
