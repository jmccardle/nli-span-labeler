"""
Seed SYNTHETIC gold (one item per reason + answerable ones) into the database
at $E13_DB / $E13_OUTPUTS, so the quiz can run in a throwaway instance. The
judgements are arbitrary: never run this against a real labelling database.

    E13_DB=/tmp/e13-ui.db uv run python tests/e2e/seed_synthetic_gold.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from live_walkthrough import synthetic_gold  # noqa: E402

from e13_labeler.db import get_db  # noqa: E402
from e13_labeler.gold import save_gold  # noqa: E402
from e13_labeler.reasons import REASONS  # noqa: E402

with get_db() as conn:
    rows = conn.execute("SELECT item_id, state, state_format, question_json FROM items "
                        "WHERE state_format = 'text' AND visibility = 'libre' ORDER BY item_id").fetchall()
    items = [(r["item_id"], r["state"], json.loads(r["question_json"])) for r in rows
             if json.loads(r["question_json"])["type"] == "choice" and len(r["state"].split()) >= 3]
    gold = synthetic_gold(items[::max(1, len(items) // 16)], REASONS)
    for item_id, body in gold.items():
        save_gold(conn, item_id, answerable=body.get("answerable", False), reasons=body["reasons"],
                  spans=body["spans"], explanation=body["explanation"])
print(f"seeded {len(gold)} synthetic gold items")
