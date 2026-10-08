#!/bin/bash
# Throwaway SINGLE_USER instance with a curated clauses batch, fake STT/agent engines, a worker,
# and the Chromium annotator walkthrough (fake microphone).
#   PLAYWRIGHT_MODULE=/path/to/node_modules/playwright tests/e2e/run_annotator_browser.sh [WORKDIR]
set -euo pipefail
cd "$(dirname "$0")/../.."
WORK="${1:-/tmp/e13-annotator}"
PORT="${PORT:-8016}"
ENGINES="${ENGINES:-8017}"
export E13_OUTPUTS="$WORK/outputs"
rm -rf "$WORK"; mkdir -p "$WORK/shots"
uv run python - "$WORK/pairs.jsonl" <<'EOF'
import json, sys
q = {"type": "choice", "instructions": "Say what the premise does to each part of the hypothesis.",
     "criteria": {"entailment": "true", "neutral": "open", "contradiction": "false"},
     "hypothesis": "The man is walking into a room."}
with open(sys.argv[1], "w") as f:
    for i in range(3):
        f.write(json.dumps({"id": f"pair{i}", "source": "snli", "split": "test",
                            "state": "A man is standing in the doorway of a building.",
                            "state_format": "text", "questions": {"clauses": q}}) + "\n")
EOF
echo "owner-pass-annot-1" | uv run python -m e13_labeler create-owner --login owner --password-stdin
uv run python -m e13_labeler import "$WORK/pairs.jsonl" --batch cur --task clauses >/dev/null
uv run python -m e13_labeler batch config cur --mode curated --overlap 1 >/dev/null
uv run python -m e13_labeler batch open cur
uv run python tests/e2e/fake_engines.py "$ENGINES" &
FAKE=$!
SINGLE_USER=1 COOKIE_SECURE=0 BACKUP_INTERVAL_HOURS=0 PORT=$PORT ./run.sh > "$WORK/server.log" 2>&1 &
SERVER=$!
E13_STT_URL="http://127.0.0.1:$ENGINES/v1/audio/transcriptions" E13_AGENT_URL="http://127.0.0.1:$ENGINES/v1" \
    uv run python -m e13_labeler worker --poll 0.5 > "$WORK/worker.log" 2>&1 &
WORKER=$!
trap 'kill $SERVER $FAKE $WORKER 2>/dev/null' EXIT
for _ in $(seq 40); do curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/auth/status" && break; sleep 0.5; done
node tests/e2e/annotator_browser.js "http://127.0.0.1:$PORT" "$WORK/shots"
