"""
Annotator mode (docs/e13/ANNOTATOR.md): utterances, the job queue, the engines
and the agent scribe. Free of FastAPI, like labelling.py.

Pipeline: utterance (audio or typed) -> transcribe job (audio only) -> agent
job -> proposal -> the owner's review. Engines are OpenAI-compatible HTTP
endpoints; when one is not configured or not answering, its jobs wait and are
retried, so recording never depends on a model being up.

    E13_STT_URL         e.g. http://127.0.0.1:4998/v1/audio/transcriptions
                        or   https://api.mistral.ai/v1/audio/transcriptions
    E13_STT_MODEL       "model" form field (Mistral: voxtral-mini-latest); unset for the local server
    E13_STT_TIMESTAMPS  "timestamp_granularities" form field (Mistral: segment)
    E13_STT_KEY_ENV     name of the env var holding the STT bearer token (e.g. MISTRAL_KEY)
    E13_AGENT_URL       e.g. http://127.0.0.1:8870/v1 or https://api.mistral.ai/v1 (…/chat/completions appended)
    E13_AGENT_MODEL     model name sent to the agent endpoint (default "agent"; e.g. mistral-small-2603)
    E13_AGENT_KEY_ENV   name of the env var holding the agent bearer token (or E13_AGENT_KEY, the token itself)

Keys are read from the environment by name, so they never land in the
database, the job errors or the logs. A 429 (rate limit) makes a job wait,
like an engine that is down.
"""

import json
import os
import socket
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import config
from .clauses import Clause, ClauseSubmission, Evidence, hypothesis_of, validate_clauses
from .db import CLAUSE_STANCES, NLI_LABELS, NOTE_CATEGORIES, RELATION_TYPES, audit
from .labelling import SubmissionError
from .parse import item_parse, nodes_for_span, outline, parse_by_id, resolve
from .render_state import render_state

AUDIO_MAX_BYTES = 50 * 1024 * 1024
ENGINE_RETRY_SECONDS = 30  # an engine that isn't up is asked again this often
ERROR_RETRY_SECONDS = 60
MAX_ATTEMPTS = 3           # real failures (bad answers); waiting for an engine doesn't count


class EngineUnavailable(Exception):
    """The engine isn't configured or isn't answering: the job waits."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def audio_dir() -> Path:
    return config.OUTPUTS_DIR / "audio"


# ============================================================================
# Utterances and jobs
# ============================================================================

def latest_annotation(conn, item_id: str, labeler_id: int, batch_id: Optional[int]):
    return conn.execute(
        """SELECT * FROM annotations WHERE item_id = ? AND labeler_id = ? AND batch_id IS ?
           ORDER BY version DESC LIMIT 1""", (item_id, labeler_id, batch_id)).fetchone()


def create_utterance(conn, item, batch, labeler: dict, *, source: str, text: Optional[str] = None,
                     audio: Optional[bytes] = None, mime: Optional[str] = None, duration_ms: Optional[int] = None,
                     kind: str = "label", proposal_id: Optional[int] = None) -> dict:
    """Store an utterance and queue its jobs: transcribe (audio) then agent, or agent (typed)."""
    if source == "audio":
        if not audio:
            raise ValueError("empty recording")
        if len(audio) > AUDIO_MAX_BYTES:
            raise ValueError("recording is too large")
    elif not (text or "").strip():
        raise ValueError("empty text")
    if kind not in ("label", "followup", "variation"):
        raise ValueError("kind must be label, followup or variation")
    parse_id, _ = item_parse(conn, item)
    latest = latest_annotation(conn, item["item_id"], labeler["id"], batch["id"])
    cur = conn.execute(
        """INSERT INTO utterances (item_id, labeler_id, batch_id, kind, source, text, duration_ms, parse_id,
                                   annotation_id, proposal_id, audio_mime, audio_bytes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (item["item_id"], labeler["id"], batch["id"], kind, source, (text or "").strip() or None, duration_ms,
         parse_id, latest["id"] if latest else None, proposal_id, mime, len(audio) if audio else None))
    uid = cur.lastrowid
    if source == "audio":
        ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/mpeg": "mp3",
               "audio/wav": "wav", "audio/x-wav": "wav"}.get((mime or "").split(";")[0].strip(), "bin")
        path = audio_dir() / item["item_id"].replace("/", "_").replace("#", "__") / f"{uid}.{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio)
        conn.execute("UPDATE utterances SET audio_path = ? WHERE id = ?", (str(path), uid))
        enqueue(conn, "transcribe", item["item_id"], labeler["id"], batch["id"], uid)
    else:
        enqueue_agent(conn, item["item_id"], labeler["id"], batch["id"], uid)
    return dict(conn.execute("SELECT * FROM utterances WHERE id = ?", (uid,)).fetchone())


def enqueue(conn, kind: str, item_id: str, labeler_id: int, batch_id, utterance_id=None) -> int:
    return conn.execute("INSERT INTO jobs (kind, item_id, labeler_id, batch_id, utterance_id) VALUES (?, ?, ?, ?, ?)",
                        (kind, item_id, labeler_id, batch_id, utterance_id)).lastrowid


def enqueue_agent(conn, item_id: str, labeler_id: int, batch_id, utterance_id=None) -> int:
    """One pending agent turn per item: a newer utterance supersedes a queued turn (it will see everything)."""
    conn.execute("""UPDATE jobs SET status = 'superseded', finished_at = ? WHERE kind = 'agent' AND item_id = ?
                    AND labeler_id = ? AND batch_id IS ? AND status IN ('queued', 'waiting')""",
                 (iso(now()), item_id, labeler_id, batch_id))
    return enqueue(conn, "agent", item_id, labeler_id, batch_id, utterance_id)


def claim(conn, worker: str):
    """Take the oldest runnable job. A job waits while an earlier job for the same item is unfinished."""
    t = iso(now())
    row = conn.execute(
        """SELECT j.* FROM jobs j WHERE j.status IN ('queued', 'waiting') AND (j.next_try_at IS NULL OR j.next_try_at <= ?)
             AND NOT EXISTS (SELECT 1 FROM jobs e WHERE e.item_id = j.item_id AND e.labeler_id = j.labeler_id
                             AND e.id < j.id AND e.status IN ('queued', 'waiting', 'running'))
           ORDER BY j.id LIMIT 1""", (t,)).fetchone()
    if row is None:
        return None
    cur = conn.execute("""UPDATE jobs SET status = 'running', worker = ?, started_at = ?
                          WHERE id = ? AND status IN ('queued', 'waiting')""", (worker, t, row["id"]))
    return row if cur.rowcount else None


def _finish(conn, job_id: int, status: str, *, error=None, result=None, engine=None, wait: Optional[int] = None):
    fields = {"status": status, "error": error, "engine": engine,
              "result_json": json.dumps(result, ensure_ascii=False) if result is not None else None}
    if status in ("done", "failed"):
        fields["finished_at"] = iso(now())
    if wait is not None:
        fields["next_try_at"] = iso(now() + timedelta(seconds=wait))
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))


# ============================================================================
# Engines (OpenAI-compatible HTTP)
# ============================================================================

def _post(url: str, body: bytes, headers: dict, timeout: int):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        if e.code in (429, 502, 503, 504):  # rate limited or down: wait and retry, don't count a failure
            raise EngineUnavailable(f"{url}: HTTP {e.code}")
        raise RuntimeError(f"{url}: HTTP {e.code}: {e.read()[:300]!r}")
    except (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError) as e:
        raise EngineUnavailable(f"{url}: {e}")


def _auth(prefix: str) -> dict:
    """Bearer header from the env var named by {prefix}_KEY_ENV (or the token in {prefix}_KEY)."""
    name = os.environ.get(f"{prefix}_KEY_ENV")
    key = os.environ.get(name) if name else os.environ.get(f"{prefix}_KEY")
    if name and not key:
        raise EngineUnavailable(f"{prefix}_KEY_ENV names {name}, which is not set in the worker's environment")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _form(boundary: str, name: str, value: str) -> bytes:
    return f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()


def transcribe_audio(path: str, mime: Optional[str]) -> dict:
    """POST the file to E13_STT_URL (multipart, field 'file'). Returns {"text", "segments", "engine"}."""
    url = os.environ.get("E13_STT_URL")
    if not url:
        raise EngineUnavailable("E13_STT_URL is not set")
    headers = _auth("E13_STT")
    boundary = uuid.uuid4().hex
    data = Path(path).read_bytes()
    name = Path(path).name
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: {(mime or 'application/octet-stream').split(';')[0]}\r\n\r\n").encode() + data + b"\r\n"
    model = os.environ.get("E13_STT_MODEL")
    if model:
        body += _form(boundary, "model", model)
    if os.environ.get("E13_STT_TIMESTAMPS"):
        body += _form(boundary, "timestamp_granularities", os.environ["E13_STT_TIMESTAMPS"])
    body += f"--{boundary}--\r\n".encode()
    out = _post(url, body, {**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
                timeout=int(os.environ.get("E13_STT_TIMEOUT", "600")))
    if not isinstance(out, dict) or "text" not in out:
        raise RuntimeError(f"unexpected transcription response: {str(out)[:200]}")
    segments = [{"start": s.get("start"), "end": s.get("end"), "text": s.get("text")}
                for s in out.get("segments") or [] if isinstance(s, dict)]
    return {"text": out["text"].strip(), "segments": segments, "engine": f"{url}#{model}" if model else url}


def _content_text(content) -> str:
    """
    Message content as text. Mistral's reasoning models return a list of chunks
    ({"type": "text"} / {"type": "thinking", ...}) instead of a string (the same
    fix as tau's openai provider); thinking is dropped.
    """
    if isinstance(content, list):
        return "".join(c if isinstance(c, str) else (c.get("text") or "") for c in content
                       if isinstance(c, str) or (isinstance(c, dict) and c.get("type") == "text"))
    return content or ""


def chat(messages: list, schema: dict) -> tuple[dict, str]:
    """One JSON-schema-constrained chat completion from E13_AGENT_URL. Returns (parsed JSON, engine)."""
    base = os.environ.get("E13_AGENT_URL")
    if not base:
        raise EngineUnavailable("E13_AGENT_URL is not set")
    model = os.environ.get("E13_AGENT_MODEL", "agent")
    url = base.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json", **_auth("E13_AGENT")}
    body = {"model": model, "messages": messages, "temperature": 0.2,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "annotation", "schema": schema, "strict": True}}}
    out = _post(url, json.dumps(body).encode(), headers, timeout=int(os.environ.get("E13_AGENT_TIMEOUT", "900")))
    try:
        content = _content_text(out["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"unexpected chat response: {str(out)[:200]}")
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    try:
        return json.loads(text), f"{base}#{model}"
    except ValueError:
        raise RuntimeError(f"agent answer is not JSON: {text[:200]!r}")


# ============================================================================
# The agent scribe
# ============================================================================

SPAN_REF = {"type": "object", "additionalProperties": False, "required": ["nodes", "phrase"],
            "properties": {"nodes": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
                           "phrase": {"type": "boolean"}}}
_NULLABLE_STANCE = {"enum": [*CLAUSE_STANCES, None]}

# The agent returns EDITS to the current annotation, never a rewrite of it: what the
# annotator didn't mention can't be dropped, and several short utterances stack as
# several small proposals. Ids (c1, c1.e1, r1) name the current view's items.
AGENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["add_clauses", "change_clauses", "remove_clauses", "add_relations", "remove_relations", "notes",
                 "label_override", "completion", "questions"],
    "properties": {
        "add_clauses": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["span", "stance", "evidence", "omission"],
            "properties": {"span": SPAN_REF, "stance": {"enum": list(CLAUSE_STANCES)},
                           "evidence": {"type": "array", "items": SPAN_REF}, "omission": {"type": "boolean"}}}},
        "change_clauses": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["clause", "stance", "omission", "span", "add_evidence", "remove_evidence"],
            "properties": {"clause": {"type": "string"}, "stance": _NULLABLE_STANCE,
                           "omission": {"type": ["boolean", "null"]},
                           "span": {"anyOf": [SPAN_REF, {"type": "null"}]},
                           "add_evidence": {"type": "array", "items": SPAN_REF},
                           "remove_evidence": {"type": "array", "items": {"type": "string"}}}}},
        "remove_clauses": {"type": "array", "items": {"type": "string"}},
        "add_relations": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["from", "to", "type", "note"],
            "properties": {"from": SPAN_REF, "to": SPAN_REF, "type": {"enum": list(RELATION_TYPES)},
                           "note": {"type": ["string", "null"]}}}},
        "remove_relations": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["nodes", "category", "text", "hedge"],
            "properties": {"nodes": {"type": "array", "items": {"type": "integer"}},
                           "category": {"enum": list(NOTE_CATEGORIES)}, "text": {"type": "string"},
                           "hedge": {"type": "boolean"}}}},
        "label_override": {"enum": [*NLI_LABELS, "clear", None]},
        "completion": {"type": ["object", "null"], "additionalProperties": False,
                       "required": ["entail", "contradict"],
                       "properties": {"entail": {"type": ["string", "null"]},
                                      "contradict": {"type": ["string", "null"]}}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = """You are a scribe for a human annotator. The annotator judges what a PREMISE says about a HYPOTHESIS and \
talks about it, naming words by their node numbers. You turn what they said into EDITS to their annotation. You never \
judge on your own: every edit must come from the annotator's words. If what they said is unclear, ask in "questions" \
instead of guessing.

The task:
- The hypothesis is split into CLAUSES (the conditions it claims). Each clause has a stance:
  supported (premise words settle it true), contradicted (premise words settle it false),
  undetermined (related premise words that don't settle it, e.g. "suggests"), unaddressed (nothing in the premise bears on it).
- supported / contradicted / undetermined clauses carry EVIDENCE: premise spans. unaddressed clauses carry none.
- omission = true ONLY if the annotator says "omission" (or "left out of the list").
- Words that claim nothing (a, the, is) stay outside clauses. Clauses never overlap.
- The label is derived: any contradicted -> contradiction; else all supported -> entailment; else neutral. Set \
label_override only if the annotator explicitly overrides the label ("clear" removes an override; null leaves it).
- completion: null (leave as is) unless the annotator DICTATES an E or C sentence for a neutral item. Never write one.

Spans: a span is a list of node numbers on ONE side (premise or hypothesis). Use phrase=false (exactly those words, \
from the first to the last) unless the annotator says "phrase", "whole", "everything under" or similar; then \
phrase=true expands each node to its subtree. Parses can be wrong, so never expand on your own.

EDITS (only for what the NEW utterances say; everything else stays exactly as it is):
- add_clauses: new clauses. A hypothesis span that overlaps an existing clause is a change to that clause, not a new one.
- change_clauses: by id ("c2"): a new stance (or null to keep it), omission (or null), a new span (or null), evidence \
to add, and evidence ids to remove ("c2.e1").
- remove_clauses / remove_relations: ids the annotator asked to remove.
- add_relations: relations they state between spans: referent, supports, suggests, contradicts, more_specific, \
less_specific, same_as, other (e.g. "2 is the referent of 9" -> from [2] to [9] referent). A clause's evidence is \
already recorded by the clause; don't repeat it as a relation unless they say so.
- notes: every remark IN THE NEW UTTERANCES about meaning, grammar, part of speech, word meanings (lexical), the label \
or the procedure, and every statement introduced with "note", in THEIR words (lightly cleaned of filler, never \
paraphrased into a different claim); hedge=true when they sounded unsure ("I'd say", "maybe", "I think"). NOTES SO \
FAR are already recorded: never repeat them.
- Each clause gets ONE edit: to change an existing clause use change_clauses only (not add_clauses as well).
Apply the NEW utterances in order; a later statement overrides an earlier one ("no, actually 15 is undetermined"). \
Earlier utterances are context only: their edits are already in the current annotation.

The utterances are speech transcripts: numbers may come as words ("thirteen") or be misheard. Map them to node \
numbers using the trees and the words the annotator names alongside them; if a number doesn't fit the words they \
describe, use the words and say so in "questions". Answer with JSON only."""


def _numbered_line(nodes: list, text: str) -> str:
    out, pos = [], 0
    for node in nodes:
        out.append(text[pos:node["start"]])
        out.append(f"{node['text']}[{node['n']}]")
        pos = node["end"]
    out.append(text[pos:])
    return "".join(out)


# ============================================================================
# Annotation state and deltas (offsets, so they apply to whatever is current)
# ============================================================================

def annotation_state(conn, ann) -> dict:
    """{"clauses", "relations", "label_override", "completion"} of a stored version (empty for None)."""
    from .clauses import completion_of, stored_clauses

    if ann is None or not ann["label"]:
        return {"clauses": [], "relations": [], "label_override": None, "completion": None}
    clauses = [{**c, "evidence": [{k: e[k] for k in ("start", "end", "text")} for e in c["evidence"]]}
               for c in stored_clauses(conn, [ann["id"]]).get(ann["id"], [])]
    relations = [{"from": json.loads(r["from_json"]), "to": json.loads(r["to_json"]), "type": r["type"],
                  "note": r["note"]}
                 for r in conn.execute("SELECT * FROM relations WHERE annotation_id = ? ORDER BY idx", (ann["id"],))]
    return {"clauses": clauses, "relations": relations,
            "label_override": ann["label"] if ann["label"] != ann["label_derived"] else None,
            "completion": completion_of(ann)}


def pending_proposals(conn, item_id: str, labeler_id: int, batch_id, through: Optional[int] = None) -> list:
    sql = ("SELECT * FROM proposals WHERE item_id = ? AND labeler_id = ? AND batch_id IS ? AND status = 'pending'"
           + (" AND id <= ?" if through is not None else "") + " ORDER BY id")
    args = (item_id, labeler_id, batch_id) + ((through,) if through is not None else ())
    return conn.execute(sql, args).fetchall()


def _same(a: dict, b: dict) -> bool:
    return a.get("start") == b.get("start") and a.get("end") == b.get("end")


def _find_clause(clauses: list, target: dict) -> Optional[int]:
    """The clause a delta targets: same offsets, else the one overlapping it most (an earlier edit may have moved it)."""
    for i, c in enumerate(clauses):
        if _same(c, target):
            return i
    best, score = None, 0
    for i, c in enumerate(clauses):
        o = min(c["end"], target["end"]) - max(c["start"], target["start"])
        if o > score:
            best, score = i, o
    return best


def apply_delta(state: dict, delta: dict) -> tuple[dict, list]:
    """Apply one resolved delta to a state. Returns (new state, problems). The input isn't modified."""
    import copy

    st = copy.deepcopy(state)
    problems = []
    clauses = st["clauses"]
    for t in delta.get("remove_clauses") or []:
        i = _find_clause(clauses, t)
        if i is None:
            problems.append(f"remove: no clause at “{t.get('text')}”")
        else:
            clauses.pop(i)
    for ch in delta.get("change_clauses") or []:
        i = _find_clause(clauses, ch["target"])
        if i is None:
            problems.append(f"change: no clause at “{ch['target'].get('text')}”")
            continue
        c = clauses[i]
        if ch.get("span"):
            c.update({k: ch["span"][k] for k in ("start", "end", "text")})
        if ch.get("stance"):
            c["stance"] = ch["stance"]
            if ch["stance"] != "contradicted":
                c["omission"] = False
            if ch["stance"] == "unaddressed":
                c["evidence"] = []
        if ch.get("omission") is not None:
            c["omission"] = bool(ch["omission"])
        c["evidence"] = [e for e in c["evidence"] if not any(_same(e, r) for r in ch.get("remove_evidence") or [])]
        for e in ch.get("add_evidence") or []:
            if not any(_same(e, x) for x in c["evidence"]):
                c["evidence"].append({k: e[k] for k in ("start", "end", "text")})
        c["evidence"].sort(key=lambda e: e["start"])
    for a in delta.get("add_clauses") or []:
        i = next((k for k, c in enumerate(clauses) if c["start"] < a["end"] and a["start"] < c["end"]), None)
        if i is not None:  # overlaps an existing clause: treat as a change of that clause (the prompt asks for this)
            problems.append(f"add “{a['text']}” overlaps clause “{clauses[i]['text']}”; replaced it")
            clauses.pop(i)
        clauses.append({k: a[k] for k in ("start", "end", "text", "stance", "omission")} |
                       {"note": None, "evidence": [{k: e[k] for k in ("start", "end", "text")} for e in a["evidence"]]})
    clauses.sort(key=lambda c: c["start"])
    rels = st["relations"]
    key = lambda r: (r["type"], r["from"].get("side"), r["from"]["start"], r["from"]["end"],
                     r["to"].get("side"), r["to"]["start"], r["to"]["end"])
    gone = {key(r) for r in delta.get("remove_relations") or []}
    st["relations"] = rels = [r for r in rels if key(r) not in gone]
    for r in delta.get("add_relations") or []:
        if key(r) not in {key(x) for x in rels}:
            rels.append(r)
    lo = delta.get("label_override")
    if lo == "clear":
        st["label_override"] = None
    elif lo:
        st["label_override"] = lo
    if delta.get("completion") is not None:
        st["completion"] = delta["completion"]
    return st, problems


def view_state(conn, item_id: str, labeler_id: int, batch_id) -> dict:
    """What the annotator is looking at: the latest version with every pending proposal applied in order."""
    st = annotation_state(conn, latest_annotation(conn, item_id, labeler_id, batch_id))
    for p in pending_proposals(conn, item_id, labeler_id, batch_id):
        st, _ = apply_delta(st, json.loads(p["payload_json"] or "{}").get("delta") or {})
    return st


def _ids(state: dict, nodes: dict) -> tuple[dict, list, list]:
    """The view the agent sees (ids c1, c1.e1, r1), and id -> target lookups for clauses and relations."""
    clauses, rels = [], []
    view = {"clauses": [], "relations": [], "label_override": state.get("label_override")}
    for i, c in enumerate(state["clauses"], start=1):
        clauses.append(c)
        view["clauses"].append({
            "id": f"c{i}", "text": c["text"], "nodes": nodes_for_span(nodes, "hypothesis", c["start"], c["end"]),
            "stance": c["stance"], "omission": c.get("omission", False),
            "evidence": [{"id": f"c{i}.e{j}", "text": e["text"],
                          "nodes": nodes_for_span(nodes, "premise", e["start"], e["end"])}
                         for j, e in enumerate(c["evidence"], start=1)]})
    for i, r in enumerate(state["relations"], start=1):
        rels.append(r)
        view["relations"].append({"id": f"r{i}", "from": r["from"].get("text"), "from_nodes": r["from"].get("nodes"),
                                  "type": r["type"], "to": r["to"].get("text"), "to_nodes": r["to"].get("nodes")})
    return view, clauses, rels


def uncovered_utterances(conn, item_id: str, labeler_id: int, batch_id) -> tuple[list, list]:
    """(all transcribed utterances, the ids no proposal covers yet). Rejected proposals cover theirs too."""
    utts = conn.execute("""SELECT * FROM utterances WHERE item_id = ? AND labeler_id = ? AND batch_id IS ?
                           AND text IS NOT NULL ORDER BY id""", (item_id, labeler_id, batch_id)).fetchall()
    covered = set()
    for p in conn.execute("SELECT utterance_ids_json FROM proposals WHERE item_id = ? AND labeler_id = ? "
                          "AND batch_id IS ? AND status != 'superseded'", (item_id, labeler_id, batch_id)):
        covered.update(json.loads(p["utterance_ids_json"]))
    return utts, [u["id"] for u in utts if u["id"] not in covered]


def agent_messages(conn, job, item, nodes: dict) -> tuple[list, list, dict]:
    """The prompt for one agent turn, the utterance ids it covers, and the view it was made against."""
    premise = render_state(item["state"], item["state_format"]).text
    hyp = hypothesis_of(json.loads(item["question_json"])) or ""
    state = view_state(conn, item["item_id"], job["labeler_id"], job["batch_id"])
    view, _, _ = _ids(state, nodes)
    notes = [f"- [{n['category']}] {n['text']}" for n in conn.execute(
        "SELECT * FROM notes WHERE item_id = ? AND labeler_id = ? AND retracted_at IS NULL ORDER BY id",
        (item["item_id"], job["labeler_id"]))]
    utts, new = uncovered_utterances(conn, item["item_id"], job["labeler_id"], job["batch_id"])
    lines = [f"({'NEW' if u['id'] in new else 'earlier'}, #{u['id']}) {u['text']}" for u in utts]
    user = "\n".join([
        "PREMISE (node numbers in brackets):", _numbered_line(nodes["premise"], premise) if nodes["premise"] and
        nodes["premise"][0]["pos"] != "FIELD" else premise, "", "Premise tree:", outline(nodes, "premise"), "",
        "HYPOTHESIS:", _numbered_line(nodes["hypothesis"], hyp), "", "Hypothesis tree:", outline(nodes, "hypothesis"),
        "", "CURRENT ANNOTATION (edit it by these ids):", json.dumps(view, ensure_ascii=False), "",
        "NOTES SO FAR:", "\n".join(notes) or "(none)", "",
        "WHAT THE ANNOTATOR SAID (oldest first; apply only the NEW ones):", "\n".join(lines) or "(nothing yet)",
    ])
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], new, state


def resolve_proposal(raw: dict, nodes: dict, item, state: dict) -> tuple[dict, list]:
    """
    Agent edits (node numbers, view ids) -> a delta in offsets, plus a preview:
    the view with this delta applied. Problems are kept with the proposal.
    """
    problems = []
    premise = render_state(item["state"], item["state_format"]).text
    hyp = hypothesis_of(json.loads(item["question_json"])) or ""
    texts = {"premise": premise, "hypothesis": hyp}
    _, clauses, rels = _ids(state, nodes)

    def span(ref, want=None, where=""):
        r = resolve(nodes, ref.get("nodes") or [], phrase=bool(ref.get("phrase", False)))
        if r is None:
            problems.append(f"{where}: nodes {ref.get('nodes')} don't resolve to one text")
            return None
        if want and r["side"] != want:
            problems.append(f"{where}: nodes {r['nodes']} are in the {r['side']}, not the {want}")
            return None
        return {**r, "text": texts[r["side"]][r["start"]:r["end"]]}

    def clause_by_id(cid: str, where: str):
        """By id ("c2"); models sometimes name the clause by its text instead, so that is accepted too."""
        s = str(cid).strip()
        if s[:1] == "c" and s[1:].split(".")[0].isdigit():
            k = int(s[1:].split(".")[0]) - 1
            if 0 <= k < len(clauses):
                return clauses[k]
        for c in clauses:
            if c["text"].strip(" .").lower() == s.strip(" .").lower():
                return c
        problems.append(f"{where}: no clause {cid!r}")
        return None

    def target(c):
        return {"start": c["start"], "end": c["end"], "text": c["text"]}

    delta = {"add_clauses": [], "change_clauses": [], "remove_clauses": [], "add_relations": [],
             "remove_relations": [], "label_override": raw.get("label_override"), "completion": raw.get("completion")}
    for i, a in enumerate(raw.get("add_clauses") or []):
        s = span(a.get("span") or {}, "hypothesis", f"new clause {i + 1}")
        if s is None:
            continue
        ev = [e for e in (span(x, "premise", f"new clause {i + 1} evidence") for x in a.get("evidence") or []) if e]
        delta["add_clauses"].append({"start": s["start"], "end": s["end"], "text": s["text"], "stance": a.get("stance"),
                                     "omission": bool(a.get("omission")),
                                     "evidence": [{k: e[k] for k in ("start", "end", "text")} for e in ev]})
    for ch in raw.get("change_clauses") or []:
        c = clause_by_id(ch.get("clause"), "change")
        if c is None:
            continue
        new_span = span(ch["span"], "hypothesis", f"change {ch.get('clause')} span") if ch.get("span") else None
        remove = []
        for eid in ch.get("remove_evidence") or []:
            try:
                e = c["evidence"][int(str(eid).split(".e")[1]) - 1]
                remove.append({"start": e["start"], "end": e["end"], "text": e["text"]})
            except (IndexError, ValueError):
                problems.append(f"change {ch.get('clause')}: no evidence {eid!r}")
        add = [e for e in (span(x, "premise", f"change {ch.get('clause')} evidence") for x in ch.get("add_evidence") or [])
               if e]
        delta["change_clauses"].append({
            "target": target(c), "stance": ch.get("stance"), "omission": ch.get("omission"),
            "span": {k: new_span[k] for k in ("start", "end", "text")} if new_span else None,
            "add_evidence": [{k: e[k] for k in ("start", "end", "text")} for e in add], "remove_evidence": remove})
    for cid in raw.get("remove_clauses") or []:
        c = clause_by_id(cid, "remove")
        if c is not None:
            delta["remove_clauses"].append(target(c))
    # An add that overlaps a clause this same turn changes or removes is the same edit twice: keep the change
    touched = [ch["target"] for ch in delta["change_clauses"]] + delta["remove_clauses"]
    delta["add_clauses"] = [a for a in delta["add_clauses"]
                            if not any(a["start"] < t["end"] and t["start"] < a["end"] for t in touched)]
    for i, r in enumerate(raw.get("add_relations") or []):
        a, b = span(r.get("from") or {}, None, f"relation {i + 1}"), span(r.get("to") or {}, None, f"relation {i + 1}")
        if a and b:
            delta["add_relations"].append({"from": a, "to": b, "type": r.get("type"), "note": r.get("note")})
    for rid in raw.get("remove_relations") or []:
        try:
            delta["remove_relations"].append(rels[int(str(rid).lstrip("r")) - 1])
        except (ValueError, IndexError):
            problems.append(f"remove relation: no relation {rid!r}")
    notes = []
    for n in raw.get("notes") or []:
        if n.get("category") not in NOTE_CATEGORIES or not (n.get("text") or "").strip():
            problems.append(f"note skipped: {str(n)[:80]}")
            continue
        notes.append({"nodes": [int(x) for x in n.get("nodes") or []], "category": n["category"],
                      "text": n["text"].strip(), "hedge": bool(n.get("hedge"))})
    preview, apply_problems = apply_delta(state, delta)
    problems += apply_problems
    # Completion sentences belong to neutral items only; drop them (noted) rather than void the proposal
    comp = delta["completion"] or {}
    if any((comp.get(k) or "").strip() for k in ("entail", "contradict")):
        from .clauses import derive_label

        label = preview["label_override"] or (derive_label(c["stance"] for c in preview["clauses"])
                                              if preview["clauses"] else None)
        if label != "neutral":
            problems.append(f"dropped completion sentences: the label is {label}, and they are for neutral items")
            delta["completion"] = None
            preview["completion"] = state.get("completion")
    payload = {"delta": delta, "notes": notes,
               "questions": [q for q in raw.get("questions") or [] if isinstance(q, str) and q.strip()],
               "preview": preview}
    sub = submission_from(preview, note=None, policy_override=True)
    try:
        validate_clauses(sub, state=item["state"], state_format=item["state_format"],
                         question=json.loads(item["question_json"]))
        payload["label_derived"], payload["label"] = sub.label_derived, sub.label
    except SubmissionError as e:
        problems.extend(e.problems)
    return payload, problems


def submission_from(payload: dict, *, note: Optional[str], policy_override: bool,
                    active_ms: Optional[int] = None) -> ClauseSubmission:
    return ClauseSubmission(
        clauses=[Clause(c["start"], c["end"], c["text"], c["stance"], omission=bool(c.get("omission")),
                        note=c.get("note"), evidence=[Evidence(e["start"], e["end"], e["text"])
                                                      for e in c.get("evidence") or []])
                 for c in payload.get("clauses") or []],
        label_override=payload.get("label_override"), completion=payload.get("completion"), note=note,
        policy_override=policy_override, active_ms=active_ms)


# ============================================================================
# Running jobs
# ============================================================================

# Each runner reads what it needs, calls its engine with no database connection
# open (a model call can take minutes; SQLite would hold its write lock), then
# writes the result in a fresh connection.

def run_transcribe(conn_factory, job) -> dict:
    with conn_factory() as conn:
        u = conn.execute("SELECT * FROM utterances WHERE id = ?", (job["utterance_id"],)).fetchone()
    out = transcribe_audio(u["audio_path"], u["audio_mime"])
    with conn_factory() as conn:
        conn.execute("UPDATE utterances SET text = ?, segments_json = ?, stt_engine = ?, transcribed_at = ? WHERE id = ?",
                     (out["text"], json.dumps(out["segments"], ensure_ascii=False), out["engine"], iso(now()), u["id"]))
        if out["text"]:
            enqueue_agent(conn, job["item_id"], job["labeler_id"], job["batch_id"], u["id"])
        _finish(conn, job["id"], "done", result={"chars": len(out["text"])}, engine=out["engine"])
    return {"chars": len(out["text"]), "engine": out["engine"]}


def _norm(text: str) -> str:
    return " ".join((text or "").lower().replace(".", " ").split())


def run_agent(conn_factory, job) -> dict:
    with conn_factory() as conn:
        item = conn.execute("SELECT * FROM items WHERE item_id = ?", (job["item_id"],)).fetchone()
        parse_id, nodes = item_parse(conn, item)
        messages, utterance_ids, state = agent_messages(conn, job, item, nodes)
        latest = latest_annotation(conn, job["item_id"], job["labeler_id"], job["batch_id"])
        if not utterance_ids:  # an earlier turn already covered everything said
            _finish(conn, job["id"], "done", result={"proposal_id": None, "skipped": "nothing new"})
            return {"proposal_id": None, "skipped": "nothing new", "engine": None}
    raw, engine = chat(messages, AGENT_SCHEMA)
    payload, problems = resolve_proposal(raw, nodes, item, state)
    with conn_factory() as conn:   # a note already recorded, or waiting in a pending proposal, isn't repeated
        seen = {_norm(n["text"]) for n in conn.execute(
            "SELECT text FROM notes WHERE item_id = ? AND labeler_id = ? AND retracted_at IS NULL",
            (job["item_id"], job["labeler_id"]))}
        for p in pending_proposals(conn, job["item_id"], job["labeler_id"], job["batch_id"]):
            seen.update(_norm(n["text"]) for n in json.loads(p["payload_json"] or "{}").get("notes") or [])
    payload["notes"] = [n for n in payload["notes"] if _norm(n["text"]) not in seen]
    with conn_factory() as conn:   # proposals stack: earlier pending ones stay pending
        pid = conn.execute(
            """INSERT INTO proposals (item_id, labeler_id, batch_id, job_id, utterance_ids_json, base_annotation_id,
                                      parse_id, raw_json, payload_json, problems_json, engine)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job["item_id"], job["labeler_id"], job["batch_id"], job["id"], json.dumps(utterance_ids),
             latest["id"] if latest else None, parse_id, json.dumps(raw, ensure_ascii=False),
             json.dumps(payload, ensure_ascii=False), json.dumps(problems, ensure_ascii=False), engine)).lastrowid
        result = {"proposal_id": pid, "problems": len(problems)}
        _finish(conn, job["id"], "done", result=result, engine=engine)
    return {**result, "engine": engine}


RUNNERS = {"transcribe": run_transcribe, "agent": run_agent}


def run_one(conn_factory, worker: str) -> Optional[dict]:
    """Claim and run one job. Returns a summary, or None when nothing is runnable."""
    with conn_factory() as conn:
        job = claim(conn, worker)
    if job is None:
        return None
    try:
        result = RUNNERS[job["kind"]](conn_factory, job)
        return {"job": job["id"], "kind": job["kind"], "status": "done", **result}
    except EngineUnavailable as e:
        with conn_factory() as conn:
            _finish(conn, job["id"], "waiting", error=str(e), wait=ENGINE_RETRY_SECONDS)
        return {"job": job["id"], "kind": job["kind"], "status": "waiting", "error": str(e)}
    except Exception as e:  # a real failure: count it, give up after MAX_ATTEMPTS
        with conn_factory() as conn:
            attempts = conn.execute("UPDATE jobs SET attempts = attempts + 1 WHERE id = ? RETURNING attempts",
                                    (job["id"],)).fetchone()[0]
            if attempts >= MAX_ATTEMPTS:
                _finish(conn, job["id"], "failed", error=f"{type(e).__name__}: {e}")
            else:
                _finish(conn, job["id"], "queued", error=f"{type(e).__name__}: {e}", wait=ERROR_RETRY_SECONDS)
        return {"job": job["id"], "kind": job["kind"], "status": "error", "error": str(e)}


def work(conn_factory, *, once: bool = False, poll: float = 5.0, log=print) -> None:
    """The worker loop: run jobs until none are runnable, then poll."""
    worker = f"{socket.gethostname()}:{os.getpid()}"
    with conn_factory() as conn:   # jobs left running by a dead worker go back to the queue
        conn.execute("UPDATE jobs SET status = 'queued', worker = NULL WHERE status = 'running'")
    while True:
        done = run_one(conn_factory, worker)
        if done:
            log(json.dumps(done, ensure_ascii=False))
            continue
        if once:
            return
        time.sleep(poll)


# ============================================================================
# Review
# ============================================================================

def store_relations(conn, annotation_id: int, relations: list) -> None:
    """A version's relations (the complete set for that version)."""
    for i, r in enumerate(relations or []):
        if r.get("type") not in RELATION_TYPES or "start" not in (r.get("from") or {}) or "start" not in (r.get("to") or {}):
            continue
        conn.execute("INSERT INTO relations (annotation_id, idx, from_json, to_json, type, note) VALUES (?, ?, ?, ?, ?, ?)",
                     (annotation_id, i, json.dumps(r["from"], ensure_ascii=False),
                      json.dumps(r["to"], ensure_ascii=False), r["type"], r.get("note")))


def store_notes(conn, annotation_id: int, item_id: str, labeler_id: int, payload: dict, *,
                proposal_id=None, utterance_id=None) -> None:
    """A proposal's new notes, attached to the version that accepted it."""
    for n in payload.get("notes") or []:
        conn.execute(
            """INSERT INTO notes (item_id, labeler_id, annotation_id, target_json, category, text, hedge, source,
                                  utterance_id, proposal_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'agent', ?, ?)""",
            (item_id, labeler_id, annotation_id, json.dumps({"nodes": n.get("nodes") or []}), n["category"], n["text"],
             int(bool(n.get("hedge"))), utterance_id, proposal_id))


def carry_relations(conn, from_annotation_id: Optional[int], to_annotation_id: int) -> None:
    """A manual save keeps the previous version's relations."""
    if not from_annotation_id:
        return
    for r in conn.execute("SELECT * FROM relations WHERE annotation_id = ? ORDER BY idx", (from_annotation_id,)):
        conn.execute("INSERT INTO relations (annotation_id, idx, from_json, to_json, type, note) VALUES (?, ?, ?, ?, ?, ?)",
                     (to_annotation_id, r["idx"], r["from_json"], r["to_json"], r["type"], r["note"]))


def log_review(conn, actor_id: int, proposal_id: int, action: str, annotation_id=None) -> None:
    audit(conn, actor_id, f"proposal_{action}", str(proposal_id), {"annotation_id": annotation_id})
