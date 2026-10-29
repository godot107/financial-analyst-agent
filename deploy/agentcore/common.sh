# Shared setup for the AgentCore deploy scripts (docs/AGENTCORE_PLAN.md). Sourced.
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib.sh"

AC_IAM_STACK="$PROJECT-agentcore-iam"
AC_BUILD_STACK="$PROJECT-agentcore-build"
AC_APP_STACK="$PROJECT-agentcore-app"
AC_DIR="$ROOT/infra/agentcore"

# shared/TAGGING.md. environment=dev: an experiment next to the prod Lambda stack.
TAGS=(project="$PROJECT" environment=dev owner=williemaize cost-center=personal
      managed-by=cloudformation component=agentcore
      repository=github.com/godot107/financial-analyst-agent)
