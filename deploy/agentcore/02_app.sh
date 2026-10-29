#!/usr/bin/env bash
# The Runtime, its endpoint and its jobs bucket, on the image built by 01_build.sh.
# Previews the change set unless --execute. Costs nothing until it's invoked
# (plus storage); invoking it spends Bedrock tokens: ask first.
source "$(dirname "$0")/common.sh"
SHA="$(git -C "$ROOT" rev-parse --short HEAD)"
IMAGE="$(stack_output "$AC_BUILD_STACK" RepositoryUri):$SHA"
aws ecr describe-images --repository-name "$PROJECT-agentcore" --image-ids imageTag="$SHA" >/dev/null 2>&1 \
  || die "no image for $SHA: run 01_build.sh"

EXECUTE=(--no-execute-changeset)
[ "${1:-}" = "--execute" ] && EXECUTE=()
scan
say "Deploying $AC_APP_STACK with $IMAGE ${EXECUTE[*]:-(executing)}"
aws cloudformation deploy --stack-name "$AC_APP_STACK" --template-file "$AC_DIR/app.yaml" \
  --no-fail-on-empty-changeset "${EXECUTE[@]}" --tags "${TAGS[@]}" \
  --parameter-overrides ProjectName="$PROJECT" ImageUri="$IMAGE" \
    RuntimeRoleArn="$(stack_output "$AC_IAM_STACK" RuntimeRoleArn)" \
    Opus45ProfileArn="$(stack_output "$AC_IAM_STACK" Opus45ProfileArn)" \
    Haiku45ProfileArn="$(stack_output "$AC_IAM_STACK" Haiku45ProfileArn)"
[ ${#EXECUTE[@]} -eq 0 ] && "$PYTHON" "$HERE/agentcore/check_tags.py"
