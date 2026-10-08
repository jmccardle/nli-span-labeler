"""
Numbered trees for annotator mode (docs/e13/ANNOTATOR.md §4).

One numbering across both texts, premise first: node 1 is the premise's first
word (or, for a JSON premise, its first field), and the hypothesis continues
after the premise's last node. A node's ``phrase`` is its subtree's span (text)
or its field (JSON), so saying "9" names a clause; ``start``/``end`` are the
word (or the key) alone.

Offsets index the canonical rendering (render_state, rule 7) for the premise and
the hypothesis string for the hypothesis, as clause and evidence offsets do.
Parses are cached per item and parser version; utterances and proposals record
the parse they were made against, so a spoken "9" keeps its meaning.
"""

import json
from functools import lru_cache
from typing import Optional

from .clauses import hypothesis_of
from .render_state import RENDERER, render_state

SPACY_MODEL = "en_core_web_sm"


@lru_cache(maxsize=1)
def _nlp():
    import spacy

    return spacy.load(SPACY_MODEL)


@lru_cache(maxsize=1)
def parser_id() -> str:
    nlp = _nlp()
    return f"spacy/{SPACY_MODEL}-{nlp.meta.get('version', '?')}+{RENDERER}"


def text_nodes(text: str) -> list:
    """Dependency nodes for one text: words only (whitespace tokens are dropped), heads as local indices."""
    doc = _nlp()(text)
    keep = [t for t in doc if not t.is_space]
    local = {t.i: k for k, t in enumerate(keep)}
    out = []
    for k, t in enumerate(keep):
        # The phrase is the subtree, without edge whitespace or punctuation (a root's
        # subtree would otherwise end in the sentence's full stop)
        left, right = t.left_edge, t.right_edge
        while (left.is_space or left.is_punct) and left.i < t.i:
            left = doc[left.i + 1]
        while (right.is_space or right.is_punct) and right.i > t.i:
            right = doc[right.i - 1]
        out.append({
            "local": k, "text": t.text, "start": t.idx, "end": t.idx + len(t.text),
            "pos": t.pos_, "tag": t.tag_, "dep": t.dep_,
            "head": None if t.head.i == t.i else local.get(t.head.i),
            "phrase": [left.idx, right.idx + len(right.text)],
        })
    return out


def json_nodes(rendered) -> list:
    """One node per rendered field: the key (or the list marker) and the whole member as the phrase."""
    out = []
    by_pointer = {}
    for k, f in enumerate(rendered.fields):
        parent = f.pointer.rsplit("/", 1)[0]
        key = f.key or (f.member[0], f.member[0] + 1)
        out.append({
            "local": k, "text": rendered.text[key[0]:key[1]], "start": key[0], "end": key[1],
            "pos": "FIELD", "tag": "string" if f.is_string else ("value" if f.value else "object"), "dep": "field",
            "head": by_pointer.get(parent), "phrase": list(f.member), "pointer": f.pointer,
        })
        by_pointer[f.pointer] = k
    return out


def build(state: str, state_format: str, question: dict) -> dict:
    """{"parser", "premise": [...], "hypothesis": [...]}, numbered 1.. across both (premise first)."""
    rendered = render_state(state, state_format)
    premise = json_nodes(rendered) if rendered.format == "json" else text_nodes(rendered.text)
    hyp = hypothesis_of(question)
    hypothesis = text_nodes(hyp) if hyp else []
    n = 0
    for side, nodes in (("premise", premise), ("hypothesis", hypothesis)):
        base = n
        for node in nodes:
            n += 1
            node["n"] = n
            node["side"] = side
            node["head_n"] = None if node["head"] is None else base + node["head"] + 1
    return {"parser": parser_id(), "premise": premise, "hypothesis": hypothesis}


def item_parse(conn, item) -> tuple[int, dict]:
    """The item's cached parse (computed and stored on first use). Returns (parse id, nodes)."""
    pid = parser_id()
    row = conn.execute("SELECT id, nodes_json FROM item_parses WHERE item_id = ? AND parser = ?",
                       (item["item_id"], pid)).fetchone()
    if row:
        return row["id"], json.loads(row["nodes_json"])
    nodes = build(item["state"], item["state_format"], json.loads(item["question_json"]))
    cur = conn.execute("INSERT INTO item_parses (item_id, parser, nodes_json) VALUES (?, ?, ?)",
                       (item["item_id"], pid, json.dumps(nodes, ensure_ascii=False)))
    return cur.lastrowid, nodes


def parse_by_id(conn, parse_id: int) -> dict:
    return json.loads(conn.execute("SELECT nodes_json FROM item_parses WHERE id = ?", (parse_id,)).fetchone()[0])


def node_index(nodes: dict) -> dict:
    return {node["n"]: node for side in ("premise", "hypothesis") for node in nodes[side]}


def resolve(nodes: dict, numbers, phrase: bool = True) -> Optional[dict]:
    """
    Node numbers -> one span: {"side", "start", "end", "nodes"}. Several numbers
    must be on one side; the span runs from the first to the last. Returns None
    for unknown numbers or numbers on both sides.
    """
    index = node_index(nodes)
    picked = [index.get(int(x)) for x in numbers] if numbers else []
    if not picked or any(p is None for p in picked):
        return None
    sides = {p["side"] for p in picked}
    if len(sides) != 1:
        return None
    ranges = [tuple(p["phrase"]) if phrase else (p["start"], p["end"]) for p in picked]
    return {"side": sides.pop(), "start": min(r[0] for r in ranges), "end": max(r[1] for r in ranges),
            "nodes": sorted(int(x) for x in numbers)}


def nodes_for_span(nodes: dict, side: str, start: int, end: int) -> list:
    """The numbers of the words a span covers (to show stored clauses in node terms)."""
    return [node["n"] for node in nodes[side] if node["start"] < end and node["end"] > start]


def outline(nodes: dict, side: str) -> str:
    """Plain-text tree for the agent prompt: one line per node, indented under its head."""
    side_nodes = nodes[side]
    children: dict = {}
    roots = []
    for node in side_nodes:
        if node["head"] is None:
            roots.append(node)
        else:
            children.setdefault(node["head"], []).append(node)
    lines = []

    def walk(node, depth):
        lines.append(f"{'  ' * depth}{node['n']} {node['text']} [{node['pos']} {node['dep']}]")
        for child in children.get(node["local"], []):
            walk(child, depth + 1)

    for r in roots:
        walk(r, 0)
    return "\n".join(lines)
