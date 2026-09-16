#!/usr/bin/env bash
# Copies the service's secrets from the project .env into SSM Parameter Store as
# SecureStrings under /<project>/. Values travel on stdin, never on the command
# line (where other processes could read them) and are never printed.
source "$(dirname "$0")/lib.sh"
need jq

ENV_FILE="$ROOT/.env"
[ -f "$ENV_FILE" ] || die "$ENV_FILE not found"

value_of() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }

# The service refuses to start without an API key. If the project .env has none,
# make one and keep it there (gitignored), so you can call the service yourself.
if [ -z "$(value_of FIN_ANALYST_API_KEYS)" ]; then
  say "No FIN_ANALYST_API_KEYS in .env; generating one named 'willie'"
  printf '\nFIN_ANALYST_API_KEYS=willie=%s\n' "$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))')" >> "$ENV_FILE"
fi

# The CLI can't read --cli-input-json from a pipe, so each request goes through a
# file only this user can read, removed on exit.
TMP="$(umask 077 && mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say "Writing secrets to SSM under /$PROJECT/"
for NAME in ANTHROPIC_API_KEY SEC_USER_AGENT FIN_ANALYST_API_KEYS ALPHAVANTAGE_KEY; do
  VALUE="$(value_of "$NAME")"
  if [ -z "$VALUE" ]; then
    echo "  $NAME: not set in .env, skipped"
    continue
  fi
  jq -n --arg name "/$PROJECT/$NAME" --arg value "$VALUE" \
    '{Name: $name, Value: $value, Type: "SecureString", Overwrite: true}' > "$TMP/request.json"
  aws ssm put-parameter --cli-input-json "file://$TMP/request.json" >/dev/null
  rm -f "$TMP/request.json"
  echo "  $NAME: stored"
done
unset VALUE
echo
echo "  Next: ./deploy/02_ecr.sh"
