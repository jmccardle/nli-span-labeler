"""
Canonical state rendering (renderer "r1"): the one string the encoder reads, the
labelling app shows, and evidence offsets index into.

Shared verbatim by the E13 labelling app (``e13_labeler/render_state.py``) and
ModernBERT-NLI-Advanced (``src/v3/render_state.py``). Change one, change both,
and bump RENDERER: stored spans name the renderer they were made on.

API_CONTRACT rule 7 (amended 2026-10-06):
- A text state renders as itself, so offsets into it are offsets into the
  caller's string.
- A JSON state (an object or array, as sent or as a string that parses to one)
  renders as indented ``key: value`` lines, keys sorted, two spaces per level:

      trace_summary:
        constraint_violations: 1
        steps: 5
      constraints:
        - Never modify production without an approved change ticket

  Strings appear verbatim (no quotes, no escaping), other scalars as JSON
  literals (``1``, ``true``, ``null``), empty containers as ``{}`` / ``[]``.
  Keys are sorted so that key order, which JSON does not define, never changes
  what the model reads.
- Evidence is computed on the rendering (character offsets, then words, then
  tokens) and mapped back to RFC 6901 pointers for the caller with
  ``range_to_evidence``: a selection inside one string value becomes that
  pointer plus offsets into the value; anything else becomes one bare pointer
  per field touched, meaning the whole field (its key and value).

Offsets are Python string indices (code points). Standard library only.
"""

import json
from dataclasses import dataclass, field
from typing import Optional

RENDERER = "r1"
INDENT = "  "


@dataclass
class Field:
    pointer: str
    key: Optional[tuple]      # (start, end) of the key; None for list items
    value: Optional[tuple]    # (start, end) of a scalar or empty container literal; None for nested containers
    member: tuple             # (start, end) from the key (or the "- " marker) to the end of the subtree
    is_string: bool = False   # the value is a string, so offsets inside it are meaningful


@dataclass
class Rendered:
    text: str
    format: str               # "text" | "json"
    fields: list = field(default_factory=list)

    def key_ranges(self) -> list:
        return [f.key for f in self.fields if f.key]


def parse_json_state(state) -> Optional[object]:
    """The JSON document of a state, or None for a text state (FR-4: objects and arrays only)."""
    if isinstance(state, (dict, list)):
        return state
    if isinstance(state, str):
        s = state.lstrip()
        if s[:1] in ("{", "["):
            try:
                doc = json.loads(state)
            except ValueError:
                return None
            return doc if isinstance(doc, (dict, list)) else None
    return None


def _token(key) -> str:
    return str(key).replace("~", "~0").replace("/", "~1")


def _scalar(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    return json.dumps(value, ensure_ascii=False)


def render_state(state, state_format: Optional[str] = None) -> Rendered:
    """
    Render a state. ``state_format`` ("text" | "json") overrides detection; a
    "json" state that doesn't parse renders as text.
    """
    doc = None if state_format == "text" else parse_json_state(state)
    if doc is None:
        text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        return Rendered(text, "text")
    parts: list[str] = []
    fields: list[Field] = []
    pos = [0]

    def emit(s: str) -> tuple:
        start = pos[0]
        parts.append(s)
        pos[0] += len(s)
        return start, pos[0]

    def walk(node, pointer: str, depth: int):
        items = (sorted(node.items(), key=lambda kv: str(kv[0])) if isinstance(node, dict)
                 else list(enumerate(node)))
        for k, v in items:
            if pos[0]:
                emit("\n")
            emit(INDENT * depth)
            ptr = f"{pointer}/{_token(k)}"
            if isinstance(node, dict):
                key = emit(str(k))
                emit(":")
                begin = key[0]
            else:
                key = None
                begin = emit("-")[0]
            nested = isinstance(v, (dict, list)) and len(v) > 0
            if nested:
                f = Field(ptr, key, None, (begin, begin))
                fields.append(f)
                walk(v, ptr, depth + 1)
                f.member = (begin, pos[0])
            else:
                emit(" ")
                value = emit(_scalar(v))
                fields.append(Field(ptr, key, value, (begin, value[1]), isinstance(v, str)))

    walk(doc, "", 0)
    return Rendered("".join(parts), "json", fields)


# ============================================================================
# Pointers <-> rendered offsets
# ============================================================================

def _field(rendered: Rendered, pointer: str) -> Field:
    for f in rendered.fields:
        if f.pointer == pointer:
            return f
    raise KeyError(f"pointer {pointer!r} does not resolve")


def pointer_to_range(rendered: Rendered, pointer: str, start: Optional[int] = None,
                     end: Optional[int] = None) -> tuple:
    """
    Rule-7 coordinates -> (start, end) in the rendering. A bare pointer is the
    whole field (key and value, or the whole subtree); offsets index into a
    string value.
    """
    f = _field(rendered, pointer)
    if start is None and end is None:
        return f.member
    if not f.is_string:
        raise KeyError(f"offsets need a string value at {pointer!r}")
    if start is None or end is None or not 0 <= start < end <= f.value[1] - f.value[0]:
        raise KeyError(f"offsets [{start}:{end}] are outside the value at {pointer!r}")
    return f.value[0] + start, f.value[0] + end


def _overlaps(a: Optional[tuple], start: int, end: int) -> bool:
    return a is not None and a[0] < end and a[1] > start


def range_to_evidence(rendered: Rendered, start: int, end: int) -> list:
    """
    (start, end) in the rendering -> rule-7 evidence for the caller.
    Text states: the range itself. JSON states: pointer + offsets when the range
    lies inside one string value; otherwise one bare pointer per field whose key
    or scalar value it touches (bare = the whole field).
    """
    text = rendered.text
    if rendered.format == "text":
        return [{"pointer": None, "start": start, "end": end, "text": text[start:end]}]
    for f in rendered.fields:
        if f.is_string and f.value[0] <= start and end <= f.value[1]:
            return [{"pointer": f.pointer, "start": start - f.value[0], "end": end - f.value[0],
                     "text": text[start:end]}]
    out = []
    for f in rendered.fields:
        if _overlaps(f.key, start, end) or _overlaps(f.value, start, end):
            out.append({"pointer": f.pointer, "start": None, "end": None,
                        "text": text[f.member[0]:f.member[1]]})
    return out


# ============================================================================
# Words and tokens (what the encoder is supervised on)
# ============================================================================

def words(text: str) -> list:
    """(start, end) of the whitespace-separated words of ``text`` (the encoder's word units)."""
    out, i, n = [], 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        j = i
        while j < n and not text[j].isspace():
            j += 1
        if j > i:
            out.append((i, j))
        i = j
    return out


def word_labels(text: str, ranges: list) -> list:
    """0/1 per whitespace word of ``text``: 1 if the word overlaps any (start, end) range."""
    return [int(any(s < e2 and e > s2 for s2, e2 in ranges)) for s, e in words(text)]


def token_labels(text: str, offsets: list, ranges: list, unit: str = "word") -> list:
    """
    0/1 per token, from the tokenizer's (start, end) character offsets.

    unit="word": a token is 1 if it shares a non-space character with a marked
      word (a word overlapping any range). This is how the evidence head is
      trained (word units, E07 finding 2).
    unit="char": a token is 1 if it shares a non-space character with a range.

    Whitespace never counts, so a BPE token's leading space doesn't mark it.
    Special tokens with (0, 0) offsets are 0.
    """
    if unit == "word":
        ranges = [w for w, y in zip(words(text), word_labels(text, ranges)) if y]
    elif unit != "char":
        raise ValueError(f"unit must be 'word' or 'char', not {unit!r}")
    out = []
    for s, e in offsets:
        hit = 0
        for rs, re_ in ranges:
            a, b = max(s, rs), min(e, re_)
            if a < b and not text[a:b].isspace():
                hit = 1
                break
        out.append(hit)
    return out
