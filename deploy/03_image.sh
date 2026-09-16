#!/usr/bin/env bash
# Builds the Lambda image and pushes it, tagged with the git commit it was built from.
source "$(dirname "$0")/lib.sh"
need docker

REPO="$(stack_output "$ECR_STACK" RepositoryUri)"
[ -n "$REPO" ] && [ "$REPO" != "None" ] || die "no repository; run ./deploy/02_ecr.sh first"

TAG="$(git -C "$ROOT" rev-parse --short HEAD)"
[ -z "$(git -C "$ROOT" status --porcelain -- fin_analyst config.yaml requirements.txt Dockerfile.lambda)" ] \
  || TAG="$TAG-dirty-$(date +%s)"

say "Building $REPO:$TAG"
# --provenance=false: Lambda rejects the multi-manifest index that buildx
# attaches attestations to by default.
docker build --platform linux/amd64 --provenance=false \
  -f "$ROOT/Dockerfile.lambda" -t "$REPO:$TAG" "$ROOT"

say "Pushing"
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%%/*}" >/dev/null
docker push "$REPO:$TAG" >/dev/null
echo "$REPO:$TAG" > "$HERE/.image"
echo "  pushed $REPO:$TAG"
echo
echo "  Next: ./deploy/04_app.sh   (shows the changes first)"
