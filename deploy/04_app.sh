#!/usr/bin/env bash
# The service stack. Without --execute it only shows what would change.
#   ./deploy/04_app.sh            preview
#   ./deploy/04_app.sh --execute  create or update
source "$(dirname "$0")/lib.sh"

[ -f "$HERE/.image" ] || die "no image pushed yet; run ./deploy/03_image.sh"
IMAGE="$(cat "$HERE/.image")"
BOUNDARY="$(stack_output "$IAM_STACK" BoundaryArn)"
[ -n "$BOUNDARY" ] && [ "$BOUNDARY" != "None" ] || die "no boundary; run ./deploy/00_iam.sh (as admin) first"

scan

ARGS=(
  --stack-name "$APP_STACK"
  --template-file "$ROOT/infra/app.yaml"
  --capabilities CAPABILITY_NAMED_IAM
  --no-fail-on-empty-changeset
  --parameter-overrides ProjectName="$PROJECT" ImageUri="$IMAGE" BoundaryArn="$BOUNDARY"
)

if [ "${1:-}" != "--execute" ]; then
  say "Previewing changes to '$APP_STACK' with image $IMAGE"
  aws cloudformation deploy "${ARGS[@]}" --no-execute-changeset >/dev/null
  CHANGE_SET="$(aws cloudformation list-change-sets --stack-name "$APP_STACK" \
    --query 'sort_by(Summaries,&CreationTime)[-1].ChangeSetId' --output text)"
  aws cloudformation describe-change-set --change-set-name "$CHANGE_SET" \
    --query 'Changes[].ResourceChange.{Action:Action,Type:ResourceType,Resource:LogicalResourceId,Replace:Replacement}' \
    --output table
  aws cloudformation delete-change-set --change-set-name "$CHANGE_SET" >/dev/null || true
  echo
  echo "  Nothing was changed. Apply with: ./deploy/04_app.sh --execute"
  exit 0
fi

say "Deploying '$APP_STACK'"
aws cloudformation deploy "${ARGS[@]}"
echo "  function URL: $(stack_output "$APP_STACK" FunctionUrl)"
echo
echo "  Next: ./deploy/05_verify.sh   (free)   or   ./deploy/05_verify.sh --memo   (~\$0.05)"
