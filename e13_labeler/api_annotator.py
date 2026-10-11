"""
Annotator-mode API (docs/e13/ANNOTATOR.md): curated batches, the item
workspace, utterances, proposals and notes. Included at the end of app.py,
after the models it reuses are defined.
"""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import annotator
from .app import AnnotationIn, _batch_filter, _save_clauses, blind_payload, fetch_visible_item
from .auth import require_active
from .clauses import completion_of, stored_clauses
from .db import NOTE_CATEGORIES, audit, get_db
from .labelling import PolicyViolation, SubmissionError
from .parse import item_parse, nodes_for_span

router = APIRouter(prefix="/api/annotator", tags=["Annotator"])


# ============================================================================
# Access
# ============================================================================

def _curated_batches(conn, labeler: dict) -> list:
    return conn.execute("SELECT * FROM batches WHERE mode = 'curated' AND status = 'open' ORDER BY priority DESC, id"
                        ).fetchall()


def _batch(conn, name: str):
    b = conn.execute("SELECT * FROM batches WHERE name = ? AND mode = 'curated' AND status = 'open'",
                     (name,)).fetchone()
    if b is None:
        raise HTTPException(404, f"no open curated batch {name!r}")
    return b


def _item_in_batch(conn, item_id: str, batch, labeler: dict):
    """The item, if it is in the batch and this labeler may see it (visibility, tier ceiling)."""
    sql, params = _batch_filter(batch, labeler)
    row = conn.execute(f"""SELECT i.* FROM batch_items bi JOIN items i ON i.item_id = bi.item_id
                           WHERE bi.batch_id = ? AND i.item_id = ? AND {sql}""",
                       (batch["id"], item_id, *params)).fetchone()
    if row is None:
        raise HTTPException(404, f"item {item_id!r} is not in batch {batch['name']!r} for you")
    return row


def _owned(conn, table: str, row_id: int, labeler: dict):
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None or row["labeler_id"] != labeler["id"]:
        raise HTTPException(404, "not found")
    return row


# ============================================================================
# Dataset list and workspace
# ============================================================================

@router.get("/batches", summary="Open curated batches")
async def batches(labeler: dict = Depends(require_active)):
    with get_db() as conn:
        out = []
        for b in _curated_batches(conn, labeler):
            sql, params = _batch_filter(b, labeler)
            n = conn.execute(f"SELECT COUNT(*) FROM batch_items bi JOIN items i ON i.item_id = bi.item_id "
                             f"WHERE bi.batch_id = ? AND {sql}", (b["id"], *params)).fetchone()[0]
            done = conn.execute("SELECT COUNT(DISTINCT item_id) FROM annotations WHERE batch_id = ? AND labeler_id = ? "
                                "AND skipped_code IS NULL", (b["id"], labeler["id"])).fetchone()[0]
            out.append({"name": b["name"], "task_type": b["task_type"], "n_items": n, "n_labelled": done})
        return {"batches": out}


def _label_flips(labels: list) -> int:
    labels = [x for x in labels if x]
    return sum(1 for a, b in zip(labels, labels[1:]) if a != b)


@router.get("/batches/{name}/items", summary="The items of a curated batch, with my status")
async def batch_items(name: str, labeler: dict = Depends(require_active)):
    with get_db() as conn:
        b = _batch(conn, name)
        sql, params = _batch_filter(b, labeler)
        rows = conn.execute(f"""SELECT i.item_id, i.state, i.state_format, i.question_json FROM batch_items bi
                                JOIN items i ON i.item_id = bi.item_id WHERE bi.batch_id = ? AND {sql}
                                ORDER BY bi.rowid""", (b["id"], *params)).fetchall()
        lid = labeler["id"]
        versions, utts, jobs, props, notes = {}, {}, {}, {}, {}
        for r in conn.execute("SELECT item_id, version, label, skipped_code FROM annotations WHERE batch_id = ? "
                              "AND labeler_id = ? ORDER BY item_id, version", (b["id"], lid)):
            versions.setdefault(r["item_id"], []).append(r)
        for r in conn.execute("SELECT item_id, COUNT(*) n, SUM(source = 'audio' AND text IS NULL) untranscribed "
                              "FROM utterances WHERE batch_id = ? AND labeler_id = ? GROUP BY item_id", (b["id"], lid)):
            utts[r["item_id"]] = r
        for r in conn.execute("SELECT item_id, status, COUNT(*) n FROM jobs WHERE batch_id = ? AND labeler_id = ? "
                              "AND status IN ('queued', 'waiting', 'running', 'failed') GROUP BY item_id, status",
                              (b["id"], lid)):
            jobs.setdefault(r["item_id"], {})[r["status"]] = r["n"]
        for r in conn.execute("SELECT item_id, COUNT(*) n FROM proposals WHERE batch_id = ? AND labeler_id = ? "
                              "AND status = 'pending' GROUP BY item_id", (b["id"], lid)):
            props[r["item_id"]] = r["n"]
        for r in conn.execute("SELECT item_id, COUNT(*) n, SUM(hedge) hedges FROM notes WHERE labeler_id = ? "
                              "AND retracted_at IS NULL GROUP BY item_id", (lid,)):
            notes[r["item_id"]] = r
        items = []
        for r in rows:
            q = json.loads(r["question_json"])
            vs = versions.get(r["item_id"], [])
            last = vs[-1] if vs else None
            u = utts.get(r["item_id"])
            n = notes.get(r["item_id"])
            items.append({
                "item_id": r["item_id"],
                "premise": r["state"][:160], "hypothesis": q.get("hypothesis"),
                "label": last["label"] if last else None,
                "skipped": last["skipped_code"] if last else None,
                "versions": len(vs), "label_flips": _label_flips([v["label"] for v in vs]),
                "utterances": u["n"] if u else 0, "untranscribed": (u["untranscribed"] or 0) if u else 0,
                "jobs": jobs.get(r["item_id"], {}), "pending_proposal": bool(props.get(r["item_id"])),
                "notes": n["n"] if n else 0, "hedges": (n["hedges"] or 0) if n else 0,
            })
        return {"batch": name, "task_type": b["task_type"], "items": items}


def _annotation_view(conn, ann, nodes: dict) -> Optional[dict]:
    if ann is None:
        return None
    clauses = stored_clauses(conn, [ann["id"]]).get(ann["id"], [])
    for c in clauses:
        c["nodes"] = nodes_for_span(nodes, "hypothesis", c["start"], c["end"])
        for e in c["evidence"]:
            e["nodes"] = nodes_for_span(nodes, "premise", e["start"], e["end"])
    relations = [{"from": json.loads(r["from_json"]), "to": json.loads(r["to_json"]), "type": r["type"],
                  "note": r["note"]}
                 for r in conn.execute("SELECT * FROM relations WHERE annotation_id = ? ORDER BY idx", (ann["id"],))]
    return {"annotation_id": ann["id"], "version": ann["version"], "label": ann["label"],
            "label_derived": ann["label_derived"], "label_override": ann["label"] if ann["label"] != ann["label_derived"]
            else None, "clauses": clauses, "relations": relations, "completion": completion_of(ann),
            "note": ann["note"], "skipped": ann["skipped_code"], "created_at": ann["created_at"],
            "active_ms": ann["active_ms"]}


@router.get("/items/{item_id:path}/workspace", summary="Everything about one item, for its annotator")
async def workspace(item_id: str, batch: str, labeler: dict = Depends(require_active)):
    with get_db() as conn:
        b = _batch(conn, batch)
        item = _item_in_batch(conn, item_id, b, labeler)
        parse_id, nodes = item_parse(conn, item)
        payload = blind_payload(conn, item, b, labeler, lock_until=None)
        lid = labeler["id"]
        latest = annotator.latest_annotation(conn, item_id, lid, b["id"])
        versions = [{"annotation_id": a["id"], "version": a["version"], "label": a["label"],
                     "created_at": a["created_at"], "active_ms": a["active_ms"], "skipped": a["skipped_code"]}
                    for a in conn.execute("SELECT * FROM annotations WHERE item_id = ? AND labeler_id = ? "
                                          "AND batch_id = ? ORDER BY version", (item_id, lid, b["id"]))]
        utterances = [{"id": u["id"], "kind": u["kind"], "source": u["source"], "text": u["text"],
                       "duration_ms": u["duration_ms"], "audio": bool(u["audio_path"]), "created_at": u["created_at"],
                       "transcribed_at": u["transcribed_at"], "proposal_id": u["proposal_id"]}
                      for u in conn.execute("SELECT * FROM utterances WHERE item_id = ? AND labeler_id = ? "
                                            "AND batch_id = ? ORDER BY id", (item_id, lid, b["id"]))]
        jobs = [{"id": j["id"], "kind": j["kind"], "status": j["status"], "error": j["error"],
                 "utterance_id": j["utterance_id"], "created_at": j["created_at"], "finished_at": j["finished_at"]}
                for j in conn.execute("SELECT * FROM jobs WHERE item_id = ? AND labeler_id = ? AND batch_id = ? "
                                      "ORDER BY id DESC LIMIT 20", (item_id, lid, b["id"]))]
        # Proposals stack: each holds edits; the view is the latest version with all of them applied in order
        proposals = [{
            "id": p["id"], "payload": json.loads(p["payload_json"] or "{}"), "problems": json.loads(p["problems_json"]),
            "utterance_ids": json.loads(p["utterance_ids_json"]), "created_at": p["created_at"],
            "base_annotation_id": p["base_annotation_id"], "stale": p["parse_id"] != parse_id}
            for p in annotator.pending_proposals(conn, item_id, lid, b["id"])]
        view = None
        if proposals:
            from .clauses import derive_label

            view = annotator.view_state(conn, item_id, lid, b["id"])
            for c in view["clauses"]:
                c["nodes"] = nodes_for_span(nodes, "hypothesis", c["start"], c["end"])
            derived = derive_label(c["stance"] for c in view["clauses"]) if view["clauses"] else None
            view["label_derived"], view["label"] = derived, view["label_override"] or derived
        notes = [{"id": n["id"], "category": n["category"], "text": n["text"], "hedge": bool(n["hedge"]),
                  "nodes": json.loads(n["target_json"]).get("nodes", []), "source": n["source"],
                  "annotation_id": n["annotation_id"], "created_at": n["created_at"]}
                 for n in conn.execute("SELECT * FROM notes WHERE item_id = ? AND labeler_id = ? AND retracted_at IS NULL "
                                       "ORDER BY id", (item_id, lid))]
        e13 = json.loads(item["e13_json"]) if item["e13_json"] else {}
        return {**payload, "batch": batch, "parse": {"id": parse_id, "nodes": nodes},
                "has_reference": "reference" in e13,
                "reference_seen_at": reference_seen_at(conn, item_id, lid),
                "annotation": _annotation_view(conn, latest, nodes), "versions": versions,
                "utterances": utterances, "jobs": jobs, "proposals": proposals,
                "proposal": proposals[-1] if proposals else None, "pending_view": view, "notes": notes}


# ============================================================================
# Saving (manual edits and accepted proposals)
# ============================================================================

def _save(conn, body: AnnotationIn, item, b, labeler: dict) -> tuple[int, int, bool]:
    if b["task_type"] != "clauses":
        raise HTTPException(422, "annotator mode labels clauses batches")
    latest = annotator.latest_annotation(conn, item["item_id"], labeler["id"], b["id"])
    version = (latest["version"] + 1) if latest else 1
    aid, override = _save_clauses(conn, body, item, b, labeler, version=version, probe=False, latest=latest)
    return aid, version, override, latest


@router.post("/items/{item_id:path}/annotations", summary="Save my annotation (a new version)")
async def save_annotation(item_id: str, batch: str, body: AnnotationIn, labeler: dict = Depends(require_active)):
    if body.item_id != item_id:
        raise HTTPException(422, "item_id doesn't match")
    with get_db() as conn:
        b = _batch(conn, batch)
        item = _item_in_batch(conn, item_id, b, labeler)
        aid, version, override, latest = _save(conn, body, item, b, labeler)
        if body.relations is not None:
            annotator.store_relations(conn, aid, body.relations)
        else:
            annotator.carry_relations(conn, latest["id"] if latest else None, aid)
        return {"status": "saved", "annotation_id": aid, "version": version, "policy_override": override}


class AcceptIn(BaseModel):
    """Optional edits: when given, they replace the result (clauses; relations too when set)."""
    edits: Optional[AnnotationIn] = None
    policy_override: bool = False
    note: Optional[str] = None
    active_ms: Optional[int] = None


@router.post("/proposals/{proposal_id}/accept",
             summary="Accept pending proposals up to this one: their edits on top of my latest version")
async def accept(proposal_id: int, body: AcceptIn, labeler: dict = Depends(require_active)):
    """
    Proposals hold edits, not snapshots. Accepting one applies it, and every
    earlier pending proposal of the item, in order, to the CURRENT latest version
    (so manual saves in between are kept), and saves the result as one new version.
    """
    with get_db() as conn:
        p = _owned(conn, "proposals", proposal_id, labeler)
        if p["status"] != "pending":
            raise HTTPException(409, f"this proposal is {p['status']}")
        b = conn.execute("SELECT * FROM batches WHERE id = ?", (p["batch_id"],)).fetchone()
        b = _batch(conn, b["name"])
        item = _item_in_batch(conn, p["item_id"], b, labeler)
        through = annotator.pending_proposals(conn, item["item_id"], labeler["id"], b["id"], through=proposal_id)
        state = annotator.annotation_state(conn, annotator.latest_annotation(conn, item["item_id"], labeler["id"], b["id"]))
        for q in through:
            state, _ = annotator.apply_delta(state, json.loads(q["payload_json"] or "{}").get("delta") or {})
        relations = state["relations"]
        if body.edits is not None:
            ann = body.edits.model_copy(update={"item_id": item["item_id"]})
            if body.edits.relations is not None:
                relations = body.edits.relations
        else:
            ann = AnnotationIn(item_id=item["item_id"], clauses=state["clauses"],
                               label_override=state["label_override"], completion=state["completion"],
                               note=body.note, policy_override=body.policy_override, active_ms=body.active_ms)
        aid, version, override, _ = _save(conn, ann, item, b, labeler)
        annotator.store_relations(conn, aid, relations)
        for q in through:
            utt = json.loads(q["utterance_ids_json"])
            annotator.store_notes(conn, aid, item["item_id"], labeler["id"], json.loads(q["payload_json"] or "{}"),
                                  proposal_id=q["id"], utterance_id=utt[-1] if utt else None)
            conn.execute("UPDATE proposals SET status = 'accepted', accepted_annotation_id = ?, reviewed_at = ? "
                         "WHERE id = ?", (aid, annotator.iso(annotator.now()), q["id"]))
            annotator.log_review(conn, labeler["id"], q["id"], "accept", aid)
        return {"status": "saved", "annotation_id": aid, "version": version, "policy_override": override,
                "accepted": [q["id"] for q in through]}


@router.post("/proposals/{proposal_id}/reject", summary="Reject a proposal (kept, marked rejected)")
async def reject(proposal_id: int, labeler: dict = Depends(require_active)):
    with get_db() as conn:
        p = _owned(conn, "proposals", proposal_id, labeler)
        if p["status"] != "pending":
            raise HTTPException(409, f"this proposal is {p['status']}")
        conn.execute("UPDATE proposals SET status = 'rejected', reviewed_at = ? WHERE id = ?",
                     (annotator.iso(annotator.now()), proposal_id))
        annotator.log_review(conn, labeler["id"], proposal_id, "reject")
        return {"status": "rejected"}


# ============================================================================
# Utterances
# ============================================================================

@router.post("/items/{item_id:path}/utterances/audio", summary="Upload a recording (raw audio body)")
async def upload_audio(item_id: str, batch: str, request: Request, kind: str = "label",
                       duration_ms: Optional[int] = None, proposal_id: Optional[int] = None,
                       labeler: dict = Depends(require_active)):
    mime = request.headers.get("content-type", "")
    if not mime.startswith("audio/"):
        raise HTTPException(415, "send the recording as the request body with an audio/* content type")
    audio = await request.body()
    with get_db() as conn:
        b = _batch(conn, batch)
        item = _item_in_batch(conn, item_id, b, labeler)
        try:
            u = annotator.create_utterance(conn, item, b, labeler, source="audio", audio=audio, mime=mime,
                                           duration_ms=duration_ms, kind=kind, proposal_id=proposal_id)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"utterance_id": u["id"], "status": "queued"}


class TextUtteranceIn(BaseModel):
    text: str = Field(..., max_length=20000)
    kind: str = "label"
    proposal_id: Optional[int] = None


@router.post("/items/{item_id:path}/utterances/text", summary="Add a typed utterance")
async def add_text(item_id: str, batch: str, body: TextUtteranceIn, labeler: dict = Depends(require_active)):
    with get_db() as conn:
        b = _batch(conn, batch)
        item = _item_in_batch(conn, item_id, b, labeler)
        try:
            u = annotator.create_utterance(conn, item, b, labeler, source="typed", text=body.text, kind=body.kind,
                                           proposal_id=body.proposal_id)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"utterance_id": u["id"], "status": "queued"}


@router.get("/utterances/{utterance_id}/audio", summary="Play back a recording")
async def get_audio(utterance_id: int, labeler: dict = Depends(require_active)):
    with get_db() as conn:
        u = _owned(conn, "utterances", utterance_id, labeler)
    if not u["audio_path"]:
        raise HTTPException(404, "no audio")
    return FileResponse(u["audio_path"], media_type=(u["audio_mime"] or "application/octet-stream").split(";")[0])


# ============================================================================
# Reference (the item's hidden e13.reference), after the labeler's own answer
# ============================================================================

def reference_seen_at(conn, item_id: str, labeler_id: int) -> Optional[str]:
    row = conn.execute("SELECT MIN(created_at) FROM audit_log WHERE action = 'reference_view' AND target = ? "
                       "AND actor_id = ?", (item_id, labeler_id)).fetchone()
    return row[0] if row else None


@router.get("/items/{item_id:path}/reference", summary="The item's reference (gold, dataset clauses, evidence)")
async def reference(item_id: str, batch: str, labeler: dict = Depends(require_active)):
    """
    Curated batches aren't blind measurement, but a reference seen before
    answering would make the answer a copy. So it opens only once the labeler has
    saved an annotation of the item; the first view is audit-logged, and the
    history export marks versions saved after it.
    """
    with get_db() as conn:
        b = _batch(conn, batch)
        item = _item_in_batch(conn, item_id, b, labeler)
        if annotator.latest_annotation(conn, item_id, labeler["id"], b["id"]) is None:
            raise HTTPException(403, "Save your own annotation first; the reference opens after it.")
        e13 = json.loads(item["e13_json"]) if item["e13_json"] else {}
        ref = e13.get("reference")
        if ref is None:
            raise HTTPException(404, "this item has no reference")
        audit(conn, labeler["id"], "reference_view", item_id, {"batch": b["name"]})
        return {"reference": ref, "first_seen_at": reference_seen_at(conn, item_id, labeler["id"])}


# ============================================================================
# Notes and the queue
# ============================================================================

class NoteIn(BaseModel):
    category: str
    text: str = Field(..., min_length=1, max_length=4000)
    nodes: list[int] = Field(default_factory=list)
    hedge: bool = False


@router.post("/items/{item_id:path}/notes", summary="Add a typed note")
async def add_note(item_id: str, batch: str, body: NoteIn, labeler: dict = Depends(require_active)):
    if body.category not in NOTE_CATEGORIES:
        raise HTTPException(422, f"category must be one of {', '.join(NOTE_CATEGORIES)}")
    with get_db() as conn:
        b = _batch(conn, batch)
        _item_in_batch(conn, item_id, b, labeler)
        latest = annotator.latest_annotation(conn, item_id, labeler["id"], b["id"])
        nid = conn.execute(
            """INSERT INTO notes (item_id, labeler_id, annotation_id, target_json, category, text, hedge, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'typed')""",
            (item_id, labeler["id"], latest["id"] if latest else None, json.dumps({"nodes": body.nodes}),
             body.category, body.text.strip(), int(body.hedge))).lastrowid
        return {"note_id": nid}


@router.post("/notes/{note_id}/retract", summary="Retract a note (kept, marked retracted)")
async def retract_note(note_id: int, labeler: dict = Depends(require_active)):
    with get_db() as conn:
        _owned(conn, "notes", note_id, labeler)
        conn.execute("UPDATE notes SET retracted_at = ? WHERE id = ? AND retracted_at IS NULL",
                     (annotator.iso(annotator.now()), note_id))
        return {"status": "retracted"}


@router.get("/queue", summary="My jobs: counts by kind and status, and the latest errors")
async def queue(labeler: dict = Depends(require_active)):
    with get_db() as conn:
        counts = {}
        for r in conn.execute("SELECT kind, status, COUNT(*) n FROM jobs WHERE labeler_id = ? GROUP BY kind, status",
                              (labeler["id"],)):
            counts.setdefault(r["kind"], {})[r["status"]] = r["n"]
        waiting = conn.execute("SELECT kind, error FROM jobs WHERE labeler_id = ? AND status = 'waiting' "
                               "ORDER BY id DESC LIMIT 1", (labeler["id"],)).fetchone()
        return {"counts": counts, "waiting_on": dict(waiting) if waiting else None}
