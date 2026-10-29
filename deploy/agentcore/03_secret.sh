#!/usr/bin/env bash
# Writes the Claude API key from the project .env into the app stack's secret, the one
# AgentCore Identity serves to the Runtime. The key goes through stdin only: never an
# argument, never printed. Run after 02_app.sh --execute; again whenever the key rotates
# (running containers keep the old key until AgentCore recycles them).
source "$(dirname "$0")/common.sh"
SECRET="$(stack_output "$AC_APP_STACK" ClaudeKeySecretArn)"
[ -n "$SECRET" ] && [ "$SECRET" != None ] || die "no secret: deploy the app stack first (02_app.sh --execute)"

say "Writing ANTHROPIC_API_KEY from .env into the Identity-backed secret"
"$PYTHON" - "$ROOT/.env" <<'PY' | aws secretsmanager put-secret-value --secret-id "$SECRET" \
  --secret-string file:///dev/stdin --query VersionId --output text >/dev/null
import json, sys
from dotenv import dotenv_values
key = dotenv_values(sys.argv[1]).get("ANTHROPIC_API_KEY")
if not key:
    sys.exit("ANTHROPIC_API_KEY is not in .env")
sys.stdout.write(json.dumps({"api_key": key}))
PY
echo "  done (the value was not shown)"
