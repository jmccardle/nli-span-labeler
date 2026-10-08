"""
The `clauses` task (docs/e13/CLAUSE_TASK.md): split the hypothesis into clauses,
give each a stance, link the premise words behind it. The sentence label is
derived, not entered.

    any clause contradicted            -> contradiction
    else every clause supported        -> entailment
    else (undetermined / unaddressed)  -> neutral

Stances, by the two questions a labeler answers per clause ("which premise words
bear on it?" and "do they settle it?"):

    supported     evidence settles it true
    contradicted  evidence settles it false; ``omission`` when the evidence is an
                  exhaustive scope that leaves the clause out ("The menu: soup,
                  salad." vs "the menu has pizza")
    undetermined  related evidence that doesn't settle it (the premise is less
                  specific: "a dog" vs "a poodle")
    unaddressed   nothing in the premise bears on it (no evidence)

Kept free of FastAPI and SQL, like labelling.py.
"""

import json
from dataclasses import dataclass, field
from typing import Optional

from .db import CLAUSE_STANCES, NLI_LABELS
from .labelling import NOTE_MAX_CHARS, PolicyViolation, SubmissionError
from .render_state import RENDERER, render_state, words

COMPLETION_KEYS = ("entail", "contradict")
COMPLETION_MAX_WORDS = 12
NEEDS_EVIDENCE = ("supported", "contradicted", "undetermined")


@dataclass
class Evidence:
    start: int
    end: int
    text: str
    renderer: Optional[str] = None


@dataclass
class Clause:
    start: int
    end: int
    text: str
    stance: str
    omission: bool = False
    note: Optional[str] = None
    evidence: list = field(default_factory=list)   # [Evidence]


@dataclass
class ClauseSubmission:
    clauses: list                       # [Clause]
    label_override: Optional[str]
    completion: Optional[dict]          # {"entail": str|None, "contradict": str|None}
    note: Optional[str]
    policy_override: bool
    active_ms: Optional[int]
    label_derived: Optional[str] = None
    label: Optional[str] = None


def hypothesis_of(question: dict) -> Optional[str]:
    h = question.get("hypothesis")
    return h if isinstance(h, str) and h.strip() else None


def derive_label(stances) -> str:
    stances = list(stances)
    if "contradicted" in stances:
        return "contradiction"
    if stances and all(s == "supported" for s in stances):
        return "entailment"
    return "neutral"


def _slice_problem(where: str, start, end, text: str, target: str) -> Optional[str]:
    if start is None or end is None or not 0 <= start < end <= len(target):
        return f"{where}: start and end must satisfy 0 <= start < end <= {len(target)}"
    if target[start:end] != text:
        return f"{where}: text {text!r} is not the slice [{start}:{end}]"
    if text != text.strip():
        return f"{where}: spans start and end on a non-space character"
    return None


def validate_clauses(sub: ClauseSubmission, *, state: str, state_format: str, question: dict) -> None:
    """Raise SubmissionError (or PolicyViolation) unless valid; sets label_derived and label."""
    hypothesis = hypothesis_of(question)
    if hypothesis is None:
        raise SubmissionError(["this item has no hypothesis to split into clauses"])
    rendered = render_state(state, state_format).text
    problems, soft = [], []

    if not sub.clauses:
        problems.append("mark at least one clause of the hypothesis")
    taken = []
    for i, c in enumerate(sub.clauses):
        where = f"clause {i + 1}"
        if c.stance not in CLAUSE_STANCES:
            problems.append(f"{where}: stance must be one of {', '.join(CLAUSE_STANCES)}")
            continue
        p = _slice_problem(where, c.start, c.end, c.text, hypothesis)
        if p:
            problems.append(p)
            continue
        if any(c.start < e and s < c.end for s, e in taken):
            problems.append(f"{where}: overlaps another clause")
        taken.append((c.start, c.end))
        if c.omission and c.stance != "contradicted":
            problems.append(f"{where}: omission is for contradicted clauses only")
        c.note = (c.note or "").strip() or None
        if c.note and len(c.note) > NOTE_MAX_CHARS:
            problems.append(f"{where}: note is longer than {NOTE_MAX_CHARS} characters")
        if c.stance == "unaddressed" and c.evidence:
            problems.append(f"{where}: an unaddressed clause has no premise evidence (use undetermined)")
        seen = set()
        for j, ev in enumerate(c.evidence):
            p = _slice_problem(f"{where} evidence {j + 1}", ev.start, ev.end, ev.text, rendered)
            if p:
                problems.append(p)
            elif (ev.start, ev.end) in seen:
                problems.append(f"{where} evidence {j + 1}: duplicate")
            seen.add((ev.start, ev.end))
            ev.renderer = RENDERER
        if c.stance in NEEDS_EVIDENCE and not c.evidence:
            soft.append(f"{where} ({c.stance}) has no premise evidence")
    # Clauses are stored in hypothesis order, whatever order they were made in
    sub.clauses.sort(key=lambda c: (c.start if c.start is not None else -1))

    sub.note = (sub.note or "").strip() or None
    if sub.note and len(sub.note) > NOTE_MAX_CHARS:
        problems.append(f"note is longer than {NOTE_MAX_CHARS} characters")
    if sub.active_ms is not None and sub.active_ms < 0:
        problems.append("active_ms must be >= 0")

    sub.label_derived = derive_label(c.stance for c in sub.clauses) if sub.clauses else None
    if sub.label_override is not None:
        if sub.label_override not in NLI_LABELS:
            problems.append(f"label_override must be one of {', '.join(NLI_LABELS)}")
        elif sub.label_override == sub.label_derived:
            sub.label_override = None  # nothing overridden
        elif not sub.note:
            problems.append("overriding the derived label needs a note saying why (e.g. a disjunction)")
    sub.label = sub.label_override or sub.label_derived

    completion = {k: ((sub.completion or {}).get(k) or "").strip() or None for k in COMPLETION_KEYS}
    unknown = set(sub.completion or {}) - set(COMPLETION_KEYS)
    if unknown:
        problems.append(f"completion keys must be {', '.join(COMPLETION_KEYS)}")
    if any(completion.values()):
        if sub.label != "neutral":
            problems.append("completion sentences are for neutral items only")
        for k, v in completion.items():
            if v and len(v.split()) > COMPLETION_MAX_WORDS:
                soft.append(f"the {k} sentence is longer than {COMPLETION_MAX_WORDS} words")
            if v and len(v) > NOTE_MAX_CHARS:
                problems.append(f"the {k} sentence is too long")
    sub.completion = completion if any(completion.values()) else None

    if problems:
        raise SubmissionError(problems)
    if soft and not sub.policy_override:
        raise PolicyViolation(soft)
    if not soft:
        sub.policy_override = False


def parse_submission(body: dict) -> ClauseSubmission:
    """API body (dicts) -> ClauseSubmission. Field types are checked by the API model."""
    return ClauseSubmission(
        clauses=[Clause(start=c.get("start"), end=c.get("end"), text=c.get("text") or "", stance=c.get("stance"),
                        omission=bool(c.get("omission")), note=c.get("note"),
                        evidence=[Evidence(e.get("start"), e.get("end"), e.get("text") or "")
                                  for e in (c.get("evidence") or [])])
                 for c in body.get("clauses") or []],
        label_override=body.get("label_override"), completion=body.get("completion"), note=body.get("note"),
        policy_override=bool(body.get("policy_override")), active_ms=body.get("active_ms"),
    )


# ============================================================================
# Storage
# ============================================================================

def insert_clauses(conn, annotation_id: int, clauses: list) -> None:
    for idx, c in enumerate(clauses):
        cid = conn.execute(
            """INSERT INTO clauses (annotation_id, idx, start, "end", text, stance, omission, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (annotation_id, idx, c.start, c.end, c.text, c.stance, int(c.omission), c.note)).lastrowid
        for ev in c.evidence:
            conn.execute('INSERT INTO clause_evidence (clause_id, start, "end", text, renderer) VALUES (?, ?, ?, ?, ?)',
                         (cid, ev.start, ev.end, ev.text, ev.renderer or RENDERER))


def stored_clauses(conn, annotation_ids) -> dict:
    """annotation id -> [clause dict with its evidence], in hypothesis order."""
    ids = list(annotation_ids)
    out: dict = {}
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        marks = ",".join("?" * len(part))
        rows = conn.execute(f"SELECT * FROM clauses WHERE annotation_id IN ({marks}) ORDER BY annotation_id, idx",
                            part).fetchall()
        evidence: dict = {}
        cids = [r["id"] for r in rows]
        for c2 in range(0, len(cids), 500):
            cpart = cids[c2:c2 + 500]
            for e in conn.execute(f"SELECT * FROM clause_evidence WHERE clause_id IN ({','.join('?' * len(cpart))}) "
                                  "ORDER BY id", cpart):
                evidence.setdefault(e["clause_id"], []).append(
                    {"start": e["start"], "end": e["end"], "text": e["text"], "renderer": e["renderer"]})
        for r in rows:
            out.setdefault(r["annotation_id"], []).append({
                "start": r["start"], "end": r["end"], "text": r["text"], "stance": r["stance"],
                "omission": bool(r["omission"]), "note": r["note"], "evidence": evidence.get(r["id"], []),
            })
    return out


def completion_of(row) -> Optional[dict]:
    return json.loads(row["completion_json"]) if row["completion_json"] else None


# ============================================================================
# Words (what the model is trained on; the E17 harness indexes text.split())
# ============================================================================

def word_indices(text: str, start: int, end: int) -> list:
    """Indices of the whitespace words of ``text`` (``text.split()``) that a span overlaps."""
    return [i for i, (s, e) in enumerate(words(text)) if s < end and e > start]


def hypothesis_word_tags(hypothesis: str, clauses: list) -> list:
    """One tag per hypothesis word: the stance of the clause covering it, or None (in no clause)."""
    tags = [None] * len(words(hypothesis))
    for c in clauses:
        for i in word_indices(hypothesis, c["start"], c["end"]):
            tags[i] = c["stance"]
    return tags


def hypothesis_word_evidence(hypothesis: str, premise: str, clauses: list) -> list:
    """Per hypothesis word: the sorted premise word indices its clause rests on ([] if none)."""
    out = [[] for _ in words(hypothesis)]
    for c in clauses:
        ev = sorted({i for e in c["evidence"] for i in word_indices(premise, e["start"], e["end"])})
        for i in word_indices(hypothesis, c["start"], c["end"]):
            out[i] = ev
    return out
