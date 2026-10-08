#!/bin/bash
# Throwaway SINGLE_USER instance with a clauses batch, and the Chromium clause walkthrough.
#   PLAYWRIGHT_MODULE=/path/to/node_modules/playwright tests/e2e/run_clause_browser.sh [WORKDIR]
set -euo pipefail
cd "$(dirname "$0")/../.."
WORK="${1:-/tmp/e13-clauses}"
PORT="${PORT:-8015}"
export E13_OUTPUTS="$WORK/outputs"
rm -rf "$WORK"; mkdir -p "$WORK/shots"
uv run python - "$WORK/pairs.jsonl" <<'EOF'
import json, sys
premise = "A dog looks up at its loving owner on the beach."
q = {"type": "choice", "instructions": "Split the hypothesis into clauses.\nTag each, then link the premise words.",
     "criteria": {"entailment": "true", "neutral": "open", "contradiction": "false"},
     "hypothesis": "A brown dog is looking up at a man."}
with open(sys.argv[1], "w") as f:
    for i in range(3):
        f.write(json.dumps({"id": f"pair{i}", "source": "snli", "split": "test", "state": premise,
                            "state_format": "text", "questions": {"clauses": q}}) + "\n")
EOF
echo "owner-pass-clauses-1" | uv run python -m e13_labeler create-owner --login owner --password-stdin
uv run python -m e13_labeler import "$WORK/pairs.jsonl" --batch nli-clauses --task clauses >/dev/null
uv run python -m e13_labeler batch open nli-clauses
SINGLE_USER=1 COOKIE_SECURE=0 BACKUP_INTERVAL_HOURS=0 PORT=$PORT ./run.sh > "$WORK/server.log" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT
for _ in $(seq 40); do curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/auth/status" && break; sleep 0.5; done
node tests/e2e/clause_browser.js "http://127.0.0.1:$PORT" "$WORK/shots"
