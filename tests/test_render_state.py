"""
The canonical renderer (render_state.py, API_CONTRACT rule 7 as amended
2026-10-06). The golden rendering below is shared with ModernBERT-NLI-Advanced
(tests/v3/test_render_state.py): if it changes, RENDERER must change too.
"""

import json

import pytest

from e13_labeler.render_state import (RENDERER, pointer_to_range, range_to_evidence, render_state, token_labels,
                                      word_labels, words)

TRACE = {
    "task": "Delete personal data for the accounts in the erasure queue.",
    "agent": {"model": "internal-agent-v4", "autonomy": "unsupervised"},
    "constraints": ["Do not touch customer data outside the named accounts",
                    "Never modify production without an approved change ticket"],
    "trace_summary": {"steps": 5, "tool_errors": 2, "constraint_violations": 1, "irreversible_actions": 1,
                      "duration_s": 447.9},
    "flags": {}, "rows": [{"a": True, "b": []}, [2, None]], "a/b": {"m~n": "x"},
}

GOLDEN = """\
a/b:
  m~n: x
agent:
  autonomy: unsupervised
  model: internal-agent-v4
constraints:
  - Do not touch customer data outside the named accounts
  - Never modify production without an approved change ticket
flags: {}
rows:
  -
    a: true
    b: []
  -
    - 2
    - null
task: Delete personal data for the accounts in the erasure queue.
trace_summary:
  constraint_violations: 1
  duration_s: 447.9
  irreversible_actions: 1
  steps: 5
  tool_errors: 2"""


def test_golden_rendering():
    assert RENDERER == "r1"
    assert render_state(json.dumps(TRACE)).text == GOLDEN


def test_key_order_and_whitespace_do_not_matter():
    """JSON doesn't order keys; the caller's serialisation never changes what the model reads."""
    shuffled = dict(reversed(list(TRACE.items())))
    assert render_state(json.dumps(shuffled, indent=4)).text == GOLDEN
    assert render_state(TRACE).text == GOLDEN


def test_text_states_render_as_themselves():
    for state in ("plain text", "Text 1: a\nText 2: b", "[not json", "42", '"a string"'):
        r = render_state(state)
        assert (r.text, r.format, r.fields) == (state, "text", [])
    assert render_state('{"a": 1}', "text").text == '{"a": 1}'   # state_format overrides detection


def test_pointer_round_trip():
    r = render_state(TRACE)
    for f in r.fields:
        start, end = pointer_to_range(r, f.pointer)
        assert (start, end) == f.member
        if f.key is None and f.value is not None:      # a scalar list item: the field is the value
            assert range_to_evidence(r, *f.value)[0]["pointer"] == f.pointer
    field = r.text.index("constraint_violations: 1")
    assert range_to_evidence(r, field, field + 24) == [
        {"pointer": "/trace_summary/constraint_violations", "start": None, "end": None,
         "text": "constraint_violations: 1"}]
    word = r.text.index("production")
    assert range_to_evidence(r, word, word + 10) == [
        {"pointer": "/constraints/1", "start": 13, "end": 23, "text": "production"}]
    assert pointer_to_range(r, "/constraints/1", 13, 23) == (word, word + 10)
    assert r.text[slice(*pointer_to_range(r, "/a~1b/m~0n"))] == "m~n: x"
    two = r.text.index("duration_s"), r.text.index("447.9") + 5
    assert [e["pointer"] for e in range_to_evidence(r, *two)] == ["/trace_summary/duration_s"]
    across = r.text.index("constraint_violations"), r.text.index("447.9") + 5
    assert [e["pointer"] for e in range_to_evidence(r, *across)] == [
        "/trace_summary/constraint_violations", "/trace_summary/duration_s"]
    with pytest.raises(KeyError):
        pointer_to_range(r, "/trace_summary/steps", 0, 1)   # offsets need a string value
    with pytest.raises(KeyError):
        pointer_to_range(r, "/nope")


def test_words_and_token_labels():
    text = "trace_summary:\n  constraint_violations: 1\n  steps: 5"
    assert [text[s:e] for s, e in words(text)] == ["trace_summary:", "constraint_violations:", "1", "steps:", "5"]
    span = (text.index("constraint"), text.index(": 1") + 3)
    assert word_labels(text, [span]) == [0, 1, 1, 0, 0]
    # BPE-style tokens carry their leading whitespace; it never marks a token.
    pieces = ["trace", "_summary", ":", "\n ", " constraint", "_viol", "ations", ":", " 1", "\n ", " steps", ":", " 5"]
    offsets, pos = [], 0
    for p in pieces:
        offsets.append((pos, pos + len(p)))
        pos += len(p)
    assert "".join(pieces) == text
    expect = [0, 0, 0, 0, 1, 1, 1, 1, 1, 0, 0, 0, 0]
    assert token_labels(text, offsets, [span]) == expect
    assert token_labels(text, [(0, 0)] + offsets + [(0, 0)], [span]) == [0] + expect + [0]   # special tokens
    # char units: only the selected characters (here without the colon after the key)
    key_only = (text.index("constraint"), text.index("constraint") + len("constraint_violations"))
    assert token_labels(text, offsets, [key_only], unit="char") == [0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0, 0]
    assert token_labels(text, offsets, [key_only], unit="word") == [0, 0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0]


def test_training_export_adds_pointers():
    """FR-46 rows keep rendered offsets (with the renderer) and give the rule-7 pointers beside them."""
    from e13_labeler.exports import add_pointers

    r = render_state(TRACE)
    field = r.text.index("constraint_violations: 1")
    spans = [{"side": "state", "start": field, "end": field + 24, "renderer": RENDERER},
             {"side": "option", "start": 0, "end": 3, "renderer": None}]
    add_pointers(spans, json.dumps(TRACE), "json")
    assert spans[0]["pointers"] == [{"pointer": "/trace_summary/constraint_violations", "start": None, "end": None}]
    assert spans[1]["pointers"] is None
    text = [{"side": "state", "start": 0, "end": 4, "renderer": RENDERER}]
    add_pointers(text, "Some text state", "text")
    assert text[0]["pointers"] is None


def test_migrate_spans_converts_pre_v8_pointer_spans(db, capsys, monkeypatch, tmp_path):
    """migrate-spans: legacy pointer spans become offsets into the rendering, audit-logged, nothing deleted."""
    monkeypatch.setenv("E13_BACKUP_DIR", str(tmp_path / "backups"))
    from e13_labeler.__main__ import main
    from e13_labeler.db import get_db

    state = json.dumps(TRACE)
    with get_db() as conn:
        conn.execute("""INSERT INTO items (item_id, row_id, qid, source, split, state, state_format, state_sha256,
                        question_json, permissions, visibility) VALUES ('t#q', 't', 'q', 'x', 'eval', ?, 'json', 'h',
                        '{"type": "noul"}', 'libre', 'libre')""", (state,))
        lid = conn.execute("INSERT INTO labelers (pseudonym, kind, role, clearance, status) "
                           "VALUES ('L09', 'human', 'labeler', 'public', 'active')").lastrowid
        aid = conn.execute("INSERT INTO annotations (item_id, labeler_id, version, answerable, reasons_json) "
                           "VALUES ('t#q', ?, 1, 1, '{}')", (lid,)).lastrowid
        for ptr, s, e, text in (("/trace_summary/steps", None, None, "steps"), ("/constraints/1", 13, 23, "production")):
            conn.execute('INSERT INTO spans (annotation_id, side, pointer, start, "end", text, role) '
                         "VALUES (?, 'state', ?, ?, ?, ?, 'support')", (aid, ptr, s, e, text))
    assert main(["migrate-spans"]) == 0
    with get_db() as conn:
        rows = conn.execute('SELECT pointer, start, "end", text, renderer FROM spans ORDER BY id').fetchall()
        audits = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'span_migrate'").fetchone()[0]
    rendered = render_state(state).text
    for r in rows:
        assert r["pointer"] is None and r["renderer"] == RENDERER and rendered[r["start"]:r["end"]] == r["text"]
    assert [r["text"] for r in rows] == ["steps: 5", "production"]
    assert audits == 2
    assert "converted 2 span(s)" in capsys.readouterr().out
