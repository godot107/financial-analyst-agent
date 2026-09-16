#!/usr/bin/env bash
# Proves the deployed service answers only signed requests, and optionally runs one
# real memo through it.
#   ./deploy/05_verify.sh          free: auth checks, health, coverage
#   ./deploy/05_verify.sh --memo   also one real memo (~$0.05)
source "$(dirname "$0")/lib.sh"

URL="$(stack_output "$APP_STACK" FunctionUrl)"
[ -n "$URL" ] && [ "$URL" != "None" ] || die "no function URL; run ./deploy/04_app.sh --execute"

API_KEY="$(grep -E '^FIN_ANALYST_API_KEYS=' "$ROOT/.env" | tail -1 | cut -d= -f2- | cut -d, -f1 | cut -d= -f2-)"
export FA_URL="$URL" FA_API_KEY="$API_KEY" FA_REGION="$REGION"
exec "$PYTHON" "$HERE/verify.py" "$@"
