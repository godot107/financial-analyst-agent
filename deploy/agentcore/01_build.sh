#!/usr/bin/env bash
# The arm64 image, built by CodeBuild on ARM from the committed code (tag = short SHA).
# Free tier aside, a small ARM build is a fraction of a cent per minute.
source "$(dirname "$0")/common.sh"
need git; need zip
[ -z "$(git -C "$ROOT" status --porcelain -- fin_analyst config.yaml requirements.txt Dockerfile.agentcore)" ] \
  || die "commit first: the image tag must name the code inside it"
SHA="$(git -C "$ROOT" rev-parse --short HEAD)"

say "Deploying $AC_BUILD_STACK"
aws cloudformation deploy --stack-name "$AC_BUILD_STACK" --template-file "$AC_DIR/build.yaml" \
  --no-fail-on-empty-changeset --tags "${TAGS[@]}" \
  --parameter-overrides ProjectName="$PROJECT" BuildRoleArn="$(stack_output "$AC_IAM_STACK" BuildRoleArn)"
BUCKET="$(stack_output "$AC_BUILD_STACK" SourceBucket)"
BUILD="$(stack_output "$AC_BUILD_STACK" ProjectName)"
REPO="$(stack_output "$AC_BUILD_STACK" RepositoryUri)"

say "Uploading source $SHA"
ARCHIVE="$(mktemp -d)/source.zip"
git -C "$ROOT" archive --format=zip -o "$ARCHIVE" HEAD
aws s3 cp --only-show-errors "$ARCHIVE" "s3://$BUCKET/source.zip"

say "Building $REPO:$SHA on ARM"
ID="$(aws codebuild start-build --project-name "$BUILD" --query build.id --output text \
  --environment-variables-override name=IMAGE_TAG,value="$SHA",type=PLAINTEXT)"
while :; do
  STATUS="$(aws codebuild batch-get-builds --ids "$ID" --query 'builds[0].buildStatus' --output text)"
  [ "$STATUS" != IN_PROGRESS ] && break
  sleep 15
done
[ "$STATUS" = SUCCEEDED ] || die "build $ID ended $STATUS: aws logs tail /aws/codebuild/$PROJECT-agentcore"
echo "image: $REPO:$SHA"
