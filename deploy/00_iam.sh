#!/usr/bin/env bash
# ADMIN, once. Creates the least-privilege deployer, the permissions boundary its
# roles must carry, and the budget alarm - then writes the deployer's access key
# straight into the AWS CLI profile the other scripts use. The key is never printed.
# Safe to re-run: the stack updates in place and a working key is left alone.
source "$(dirname "$0")/lib.sh"
need jq

USER_NAME="$PROJECT-deployer"
admin() { aws --profile "$ADMIN_PROFILE" "$@"; }

say "Checking admin credentials (profile '$ADMIN_PROFILE')"
ADMIN_ARN="$(admin sts get-caller-identity --query Arn --output text)" \
  || die "profile '$ADMIN_PROFILE' is not signed in. Run: aws login --profile $ADMIN_PROFILE"
echo "  $ADMIN_ARN"
case "$ADMIN_ARN" in *":user/$USER_NAME") die "ADMIN_PROFILE is the deployer itself; use an admin profile" ;; esac
[ "$ADMIN_PROFILE" != "$AWS_PROFILE" ] || die "ADMIN_PROFILE and AWS_PROFILE must differ"

scan

say "Deploying '$IAM_STACK' (deployer, boundary, \$$BUDGET_USD/month alarm to $BUDGET_EMAIL)"
admin cloudformation deploy \
  --stack-name "$IAM_STACK" \
  --template-file "$ROOT/infra/iam.yaml" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides ProjectName="$PROJECT" BudgetEmail="$BUDGET_EMAIL" BudgetLimitUsd="$BUDGET_USD"

say "Checking profile '$AWS_PROFILE'"
if CURRENT="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null)" \
   && [[ "$CURRENT" == *":user/$USER_NAME" ]]; then
  echo "  already authenticates as $CURRENT, keeping its key"
else
  KEYS="$(admin iam list-access-keys --user-name "$USER_NAME" --query 'length(AccessKeyMetadata)' --output text)"
  [ "$KEYS" -lt 2 ] || die "$USER_NAME already has 2 access keys; delete one in the IAM console first"

  say "Creating an access key for $USER_NAME"
  KEY_JSON="$(admin iam create-access-key --user-name "$USER_NAME" --output json)"
  aws configure set aws_access_key_id "$(jq -r .AccessKey.AccessKeyId <<<"$KEY_JSON")" --profile "$AWS_PROFILE"
  aws configure set aws_secret_access_key "$(jq -r .AccessKey.SecretAccessKey <<<"$KEY_JSON")" --profile "$AWS_PROFILE"
  unset KEY_JSON
  echo "  written to ~/.aws/credentials [$AWS_PROFILE]"

  printf '  waiting for the new key to become usable'
  for _ in $(seq 1 12); do
    aws sts get-caller-identity >/dev/null 2>&1 && break
    printf '.'; sleep 5
  done
  echo
fi
aws configure set region "$REGION" --profile "$AWS_PROFILE"
aws configure set output json --profile "$AWS_PROFILE"

say "Verifying the deployer is as narrow as intended"
aws sts get-caller-identity --query Arn --output text \
  || die "profile '$AWS_PROFILE' does not authenticate yet; IAM can take a minute, re-run to check"
if aws iam list-users >/dev/null 2>&1; then
  die "deployer can call iam:ListUsers; its policy is broader than infra/iam.yaml"
fi
echo "  IAM listing denied, as intended"
if aws cloudformation describe-stack-resources --stack-name "$IAM_STACK" >/dev/null 2>&1; then
  die "deployer can read the bootstrap stack's resources; its stack grant is too broad"
fi
echo "  bootstrap stack out of reach, as intended"
echo
echo "  Check $BUDGET_EMAIL for AWS's budget notification confirmation if one arrives."
echo "  Next: ./deploy/01_secrets.sh"
