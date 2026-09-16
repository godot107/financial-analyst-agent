# Shared setup for the deploy scripts. Sourced, not executed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"

[ -f "$HERE/config.env" ] || { echo "ERROR: $HERE/config.env missing. Copy config.env.example and edit it." >&2; exit 1; }
# shellcheck disable=SC1091
source "$HERE/config.env"

: "${PROJECT:?}" "${REGION:?}" "${BUDGET_EMAIL:?}" "${BUDGET_USD:?}"
export AWS_PROFILE="${AWS_PROFILE:-fin-analyst}"
export AWS_DEFAULT_REGION="$REGION"
ADMIN_PROFILE="${ADMIN_PROFILE:-default}"

IAM_STACK="$PROJECT-iam"
ECR_STACK="$PROJECT-ecr"
APP_STACK="$PROJECT-app"
PYTHON="$ROOT/.venv/bin/python"

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "$1 is required but not installed"; }

stack_output() {  # stack_output STACK KEY
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}

scan() {
  say "Linting and cost-scanning the templates"
  "$ROOT/.venv/bin/cfn-lint" "$ROOT"/infra/*.yaml || die "templates failed lint"
  if command -v cloudburn >/dev/null; then
    cloudburn scan "$ROOT/infra" --config "$ROOT/.cloudburn.yml" >/dev/null \
      || die "CloudBurn found a high-severity cost problem: cloudburn scan infra --config .cloudburn.yml"
    echo "  lint clean, no high cost findings"
  fi
}
