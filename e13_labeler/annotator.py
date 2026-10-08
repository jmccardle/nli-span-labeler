"""
Annotator mode (docs/e13/ANNOTATOR.md): utterances, the job queue, the engines
and the agent scribe. Free of FastAPI, like labelling.py.

Pipeline: utterance (audio or typed) -> transcribe job (audio only) -> agent
job -> proposal -> the owner's review. Engines are OpenAI-compatible HTTP
endpoints; when one is not configured or not answering, its jobs wait and are
retried, so recording never depends on a model being up.

    E13_STT_URL      e.g. http://127.0.0.1:4998/v1/audio/transcriptions
    E13_AGENT_URL    e.g. http://127.0.0.1:8870/v1   (…/chat/completions is appended)
    E13_AGENT_MODEL  model name sent to the agent endpoint (default "agent")
    E13_AGENT_KEY    optional bearer token
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
        if e.code in (502, 503, 504):
            raise EngineUnavailable(f"{url}: HTTP {e.code}")
        raise RuntimeError(f"{url}: HTTP {e.code}: {e.read()[:300]!r}")
    except (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError) as e:
        raise EngineUnavailable(f"{url}: {e}")


def transcribe_audio(path: str, mime: Optional[str]) -> dict:
    """POST the file to E13_STT_URL (multipart, field 'file'). Returns {"text", "segments", "engine"}."""
    url = os.environ.get("E13_STT_URL")
    if not url:
        raise EngineUnavailable("E13_STT_URL is not set")
    boundary = uuid.uuid4().hex
    data = Path(path).read_bytes()
    name = Path(path).name
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: {mime or 'application/octet-stream'}\r\n\r\n").encode() + data + \
           (f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\nverbose_json"
            f"\r\n--{boundary}--\r\n").encode()
    out = _post(url, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"},
                timeout=int(os.environ.get("E13_STT_TIMEOUT", "600")))
    if not isinstance(out, dict) or "text" not in out:
        raise RuntimeError(f"unexpected transcription response: {str(out)[:200]}")
    segments = [{"start": s.get("start"), "end": s.get("end"), "text": s.get("text")}
                for s in out.get("segments") or [] if isinstance(s, dict)]
    return {"text": out["text"].strip(), "segments": segments, "engine": url}


def chat(messages: list, schema: dict) -> tuple[dict, str]:
    """One JSON-schema-constrained chat completion from E13_AGENT_URL. Returns (parsed JSON, engine)."""
    base = os.environ.get("E13_AGENT_URL")
    if not base:
        raise EngineUnavailable("E13_AGENT_URL is not set")
    model = os.environ.get("E13_AGENT_MODEL", "agent")
    url = base.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if os.environ.get("E13_AGENT_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['E13_AGENT_KEY']}"
    body = {"model": model, "messages": messages, "temperature": 0.2,
            "response_format": {"type": "json_schema", "json_schema": {"name": "annotation", "schema": schema}}}
    out = _post(url, json.dumps(body).encode(), headers, timeout=int(os.environ.get("E13_AGENT_TIMEOUT", "900")))
    try:
        content = out["choices"][0]["message"]["content"]
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
AGENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["clauses", "relations", "notes", "label_override", "completion", "questions"],
    "properties": {
        "clauses": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["span", "stance", "evidence", "omission"],
            "properties": {"span": SPAN_REF, "stance": {"enum": list(CLAUSE_STANCES)},
                           "evidence": {"type": "array", "items": SPAN_REF}, "omission": {"type": "boolean"}}}},
        "relations": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["from", "to", "type", "note"],
            "properties": {"from": SPAN_REF, "to": SPAN_REF, "type": {"enum": list(RELATION_TYPES)},
                           "note": {"type": ["string", "null"]}}}},
        "notes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["nodes", "category", "text", "hedge"],
            "properties": {"nodes": {"type": "array", "items": {"type": "integer"}},
                           "category": {"enum": list(NOTE_CATEGORIES)}, "text": {"type": "string"},
                           "hedge": {"type": "boolean"}}}},
        "label_override": {"enum": [*NLI_LABELS, None]},
        "completion": {"type": ["object", "null"], "additionalProperties": False,
                       "properties": {"entail": {"type": ["string", "null"]},
                                      "contradict": {"type": ["string", "null"]}}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = """You are a scribe for a human annotator. The annotator judges what a PREMISE says about a HYPOTHESIS and \
talks about it, naming words by their node numbers. You turn what they said into a structured annotation. You never \
judge on your own: everything you write must come from the annotator's words. If they didn't say something, leave it \
as it was; if what they said is unclear, ask in "questions" instead of guessing.

The task:
- The hypothesis is split into CLAUSES (the conditions it claims). Each clause has a stance:
  supported (premise words settle it true), contradicted (premise words settle it false),
  undetermined (related premise words that don't settle it, e.g. "suggests"), unaddressed (nothing in the premise bears on it).
- supported / contradicted / undetermined clauses carry EVIDENCE: premise spans. unaddressed clauses carry none.
- omission = true only for a contradicted clause whose evidence is an exhaustive premise scope that leaves it out.
- Words that claim nothing (a, the, is) stay outside clauses. Clauses never overlap.
- The label is derived: any contradicted -> contradiction; else all supported -> entailment; else neutral. Set \
label_override only if the annotator explicitly overrides that.
- completion: for neutral items, sentences the annotator dictated that would make it entailment ("entail") or \
contradiction ("contradict").

Spans: a span is a list of node numbers on ONE side (premise or hypothesis). phrase=true means each node's whole \
phrase (its subtree); phrase=false means exactly those words. "9" usually means node 9's phrase; "word 9" means the word.

Also record:
- relations the annotator states between spans: referent, supports, suggests, contradicts, more_specific, \
less_specific, same_as, other (e.g. "2 is the referent of 9" -> from [2] to [9] referent);
- notes: what they said about a relation, the grammar, part-of-speech tags, word meanings (lexical), the label, \
or the procedure. Only NEW notes from the new utterances; keep their wording; hedge=true when they sounded unsure.

Return the COMPLETE clause list and relation list after applying the new utterances (keep earlier ones unless the \
annotator changed them). Answer with JSON only."""


def _numbered_line(nodes: list, text: str) -> str:
    out, pos = [], 0
    for node in nodes:
        out.append(text[pos:node["start"]])
        out.append(f"{node['text']}[{node['n']}]")
        pos = node["end"]
    out.append(text[pos:])
    return "".join(out)


def _span_ref(nodes: dict, side: str, start: int, end: int) -> dict:
    return {"nodes": nodes_for_span(nodes, side, start, end), "phrase": False}


def agent_messages(conn, job, item, nodes: dict) -> tuple[list, list]:
    """The prompt for one agent turn, and the utterance ids it covers."""
    from .clauses import stored_clauses

    premise = render_state(item["state"], item["state_format"]).text
    hyp = hypothesis_of(json.loads(item["question_json"])) or ""
    latest = latest_annotation(conn, item["item_id"], job["labeler_id"], job["batch_id"])
    current = {"clauses": [], "relations": [], "label": None}
    if latest is not None and latest["label"]:
        for c in stored_clauses(conn, [latest["id"]]).get(latest["id"], []):
            current["clauses"].append({
                "span": _span_ref(nodes, "hypothesis", c["start"], c["end"]), "text": c["text"],
                "stance": c["stance"], "omission": c["omission"],
                "evidence": [{**_span_ref(nodes, "premise", e["start"], e["end"]), "text": e["text"]}
                             for e in c["evidence"]]})
        for r in conn.execute("SELECT * FROM relations WHERE annotation_id = ? ORDER BY idx", (latest["id"],)):
            current["relations"].append({"from": json.loads(r["from_json"]), "to": json.loads(r["to_json"]),
                                         "type": r["type"], "note": r["note"]})
        current["label"] = latest["label"]
    notes = [f"- [{n['category']}] {n['text']}" for n in conn.execute(
        "SELECT * FROM notes WHERE item_id = ? AND labeler_id = ? AND retracted_at IS NULL ORDER BY id",
        (item["item_id"], job["labeler_id"]))]
    utts = conn.execute(
        """SELECT * FROM utterances WHERE item_id = ? AND labeler_id = ? AND batch_id IS ? AND text IS NOT NULL
           ORDER BY id""", (item["item_id"], job["labeler_id"], job["batch_id"])).fetchall()
    seen = set()
    for p in conn.execute("SELECT utterance_ids_json FROM proposals WHERE item_id = ? AND labeler_id = ? "
                          "AND status = 'accepted'", (item["item_id"], job["labeler_id"])):
        seen.update(json.loads(p["utterance_ids_json"]))
    lines = []
    for u in utts:
        tag = "earlier" if u["id"] in seen else "NEW"
        lines.append(f"({tag}, {u['kind']}, #{u['id']}) {u['text']}")
    user = "\n".join([
        "PREMISE (node numbers in brackets):", _numbered_line(nodes["premise"], premise) if nodes["premise"] and
        nodes["premise"][0]["pos"] != "FIELD" else premise, "", "Premise tree:", outline(nodes, "premise"), "",
        "HYPOTHESIS:", _numbered_line(nodes["hypothesis"], hyp), "", "Hypothesis tree:", outline(nodes, "hypothesis"),
        "", "CURRENT ANNOTATION:", json.dumps(current, ensure_ascii=False), "",
        "NOTES SO FAR:", "\n".join(notes) or "(none)", "",
        "WHAT THE ANNOTATOR SAID (oldest first):", "\n".join(lines) or "(nothing yet)",
    ])
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], [u["id"] for u in utts]


def resolve_proposal(raw: dict, nodes: dict, item) -> tuple[dict, list]:
    """Agent JSON (node numbers) -> payload with offsets, and the problems found (the proposal is kept anyway)."""
    problems = []
    premise = render_state(item["state"], item["state_format"]).text
    hyp = hypothesis_of(json.loads(item["question_json"])) or ""
    texts = {"premise": premise, "hypothesis": hyp}

    def span(ref, want=None, where=""):
        r = resolve(nodes, ref.get("nodes") or [], phrase=bool(ref.get("phrase", True)))
        if r is None:
            problems.append(f"{where}: nodes {ref.get('nodes')} don't resolve to one text")
            return None
        if want and r["side"] != want:
            problems.append(f"{where}: nodes {r['nodes']} are in the {r['side']}, not the {want}")
            return None
        return {**r, "text": texts[r["side"]][r["start"]:r["end"]]}

    clauses = []
    for i, c in enumerate(raw.get("clauses") or []):
        s = span(c.get("span") or {}, "hypothesis", f"clause {i + 1}")
        if s is None:
            continue
        ev = [e for e in (span(x, "premise", f"clause {i + 1} evidence") for x in c.get("evidence") or []) if e]
        clauses.append({"start": s["start"], "end": s["end"], "text": s["text"], "nodes": s["nodes"],
                        "stance": c.get("stance"), "omission": bool(c.get("omission")), "note": None,
                        "evidence": [{"start": e["start"], "end": e["end"], "text": e["text"], "nodes": e["nodes"]}
                                     for e in ev]})
    relations = []
    for i, r in enumerate(raw.get("relations") or []):
        a, b = span(r.get("from") or {}, None, f"relation {i + 1}"), span(r.get("to") or {}, None, f"relation {i + 1}")
        if a and b:
            if r.get("type") not in RELATION_TYPES:
                problems.append(f"relation {i + 1}: unknown type {r.get('type')!r}")
                continue
            relations.append({"from": a, "to": b, "type": r["type"], "note": r.get("note")})
    notes = []
    for n in raw.get("notes") or []:
        if n.get("category") not in NOTE_CATEGORIES or not (n.get("text") or "").strip():
            problems.append(f"note skipped: {str(n)[:80]}")
            continue
        notes.append({"nodes": [int(x) for x in n.get("nodes") or []], "category": n["category"],
                      "text": n["text"].strip(), "hedge": bool(n.get("hedge"))})
    payload = {"clauses": clauses, "relations": relations, "notes": notes,
               "label_override": raw.get("label_override"), "completion": raw.get("completion"),
               "questions": [q for q in raw.get("questions") or [] if isinstance(q, str) and q.strip()]}
    # Check the clause core the way a save would (soft rules are reported, not enforced here)
    sub = submission_from(payload, note=None, policy_override=True)
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


def run_agent(conn_factory, job) -> dict:
    with conn_factory() as conn:
        item = conn.execute("SELECT * FROM items WHERE item_id = ?", (job["item_id"],)).fetchone()
        parse_id, nodes = item_parse(conn, item)
        messages, utterance_ids = agent_messages(conn, job, item, nodes)
        latest = latest_annotation(conn, job["item_id"], job["labeler_id"], job["batch_id"])
    raw, engine = chat(messages, AGENT_SCHEMA)
    payload, problems = resolve_proposal(raw, nodes, item)
    with conn_factory() as conn:
        conn.execute("""UPDATE proposals SET status = 'superseded' WHERE item_id = ? AND labeler_id = ?
                        AND batch_id IS ? AND status = 'pending'""", (job["item_id"], job["labeler_id"], job["batch_id"]))
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

def store_extras(conn, annotation_id: int, item_id: str, labeler_id: int, payload: dict, *,
                 proposal_id=None, utterance_id=None) -> None:
    """Relations (the complete set, per version) and new notes, attached to an annotation version."""
    for i, r in enumerate(payload.get("relations") or []):
        conn.execute("INSERT INTO relations (annotation_id, idx, from_json, to_json, type, note) VALUES (?, ?, ?, ?, ?, ?)",
                     (annotation_id, i, json.dumps(r["from"], ensure_ascii=False),
                      json.dumps(r["to"], ensure_ascii=False), r["type"], r.get("note")))
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
