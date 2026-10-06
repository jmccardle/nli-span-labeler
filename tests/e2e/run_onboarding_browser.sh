#!/bin/bash
# Throwaway instance (real libre sample + SYNTHETIC gold) and the Chromium onboarding walkthrough.
#   PLAYWRIGHT_MODULE=/path/to/node_modules/playwright tests/e2e/run_onboarding_browser.sh [WORKDIR]
set -euo pipefail
cd "$(dirname "$0")/../.."
WORK="${1:-/tmp/e13-browser}"
PORT="${PORT:-8014}"
PW="owner-pass-ui-1234"
export E13_OUTPUTS="$WORK/outputs"
rm -rf "$WORK"; mkdir -p "$WORK/shots"
echo "$PW" | uv run python -m e13_labeler create-owner --login owner --password-stdin
uv run python -m e13_labeler import docs/e13/fixtures/pool_eval_libre_sample.jsonl --batch pilot >/dev/null
uv run python tests/e2e/seed_synthetic_gold.py
uv run python -m e13_labeler batch open pilot
COOKIE_SECURE=0 BACKUP_INTERVAL_HOURS=0 PORT=$PORT ./run.sh > "$WORK/server.log" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT
for _ in $(seq 40); do curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/auth/status" && break; sleep 0.5; done
node tests/e2e/onboarding_browser.js "http://127.0.0.1:$PORT" owner "$PW" "$WORK/shots"
