#!/bin/bash
# Fresh throwaway DB + server on PORT (default 8013), then the live walkthrough; stops the server after.
#   tests/e2e/run_live_walkthrough.sh [OUTPUTS_DIR]
set -euo pipefail
cd "$(dirname "$0")/../.."
export E13_OUTPUTS="${1:-/tmp/e13-e2e}"
PORT="${PORT:-8013}"
PW="owner-pass-e2e-123"
rm -rf "$E13_OUTPUTS"; mkdir -p "$E13_OUTPUTS"
echo "$PW" | uv run python -m e13_labeler create-owner --login owner --password-stdin
uv run python -m e13_labeler import docs/e13/fixtures/pool_eval_libre_sample.jsonl --batch pilot
# Plain HTTP on loopback: cookies without Secure; every simulated user shares one IP, so lift the login limit.
COOKIE_SECURE=0 RATE_LIMIT_AUTH=100/minute BACKUP_INTERVAL_HOURS=0 PORT=$PORT ./run.sh > "$E13_OUTPUTS/server.log" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT
for _ in $(seq 40); do curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/auth/status" && break; sleep 0.5; done
uv run python tests/e2e/live_walkthrough.py --base "http://127.0.0.1:$PORT" --owner owner --password "$PW" \
    --outputs "$E13_OUTPUTS"
