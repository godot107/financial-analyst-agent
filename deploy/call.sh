#!/usr/bin/env bash
# Signed HTTPS calls to the deployed service, for trying it by hand. See deploy/call.py.
#   ./deploy/call.sh GET /v1/health
#   ./deploy/call.sh POST /v1/memos '{"ticker": "MSFT", "question": "How liquid is Microsoft?"}'
#   ./deploy/call.sh wait <job id>
source "$(dirname "$0")/lib.sh"

URL="${FA_URL:-$(stack_output "$APP_STACK" FunctionUrl)}"
[ -n "$URL" ] && [ "$URL" != "None" ] || die "no function URL; run ./deploy/04_app.sh --execute"

API_KEY="$(grep -E '^FIN_ANALYST_API_KEYS=' "$ROOT/.env" | tail -1 | cut -d= -f2- | cut -d, -f1 | cut -d= -f2-)"
export FA_URL="$URL" FA_API_KEY="$API_KEY" FA_REGION="$REGION"
exec "$PYTHON" "$HERE/call.py" "$@"
