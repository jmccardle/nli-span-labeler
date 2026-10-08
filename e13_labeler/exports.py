"""
Exports (requirements FR-45 to FR-49, §5.4, §5.5).

``write_export`` writes one directory per run, under
``outputs/e13_labeler/exports/<timestamp>/``, and never deletes or overwrites
anything (owner policy: keep all artifacts). It can contain:

- ``annotations.jsonl``: one row per (item, labeler, batch), latest version (FR-45, §5.4)
- ``training.jsonl``: one row per item with enough human labels (FR-46, §5.5)
- ``agreement.json``: every agreement number plus the records behind them (FR-48)
- ``items.jsonl``: the items in the E09 pool import format, so the data round-trips (FR-5)
- ``clauses.jsonl``: clauses-batch labels, one row per (item, labeler, batch) (docs/e13/CLAUSE_TASK.md)
- ``manifest.json``: app version, guideline versions, DB checksum, filters, files (FR-49)
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .render_state import range_to_evidence, render_state
from . import config
from .analysis import agreement_record, report
from .db import audit
from .reasons import REASONS
from .records import Filters, item_filter_sql, load_annotations, public

TRAIN_SCHEMA = "e13.train/1"
MANIFEST_SCHEMA = "e13.export-manifest/1"
KINDS = ("annotations", "training", "agreement", "items", "clauses", "history")


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


# ============================================================================
# FR-45 annotations
# ============================================================================

def annotation_rows(conn: sqlite3.Connection, filters: Filters) -> list[dict]:
    return [public(r) for r in load_annotations(conn, filters)]


# ============================================================================
# FR-46 training rows
# ============================================================================

def _span_key(s: dict) -> tuple:
    return (s["side"], s["pointer"], s["start"], s["end"], s["role"], s["option"])


def merge_spans(records: Sequence[dict]) -> tuple[list, list]:
    """
    Merge spans by exact coordinates (word-snapped in the UI). ``votes`` counts
    labelers. Returns (abstain evidence: spans linked to reasons, plain evidence).
    """
    merged: dict = {}
    for r in records:
        for s in r["spans"]:
            m = merged.setdefault(_span_key(s), {"span": s, "labelers": set(), "reasons": set()})
            m["labelers"].add(r["labeler"])
            m["reasons"].update(s["reasons"])
    abstain, plain = [], []
    for m in merged.values():
        s = m["span"]
        out = {"side": s["side"], "pointer": s["pointer"], "start": s["start"], "end": s["end"],
               "text": s["text"], "role": s["role"], "option": s["option"], "renderer": s.get("renderer")}
        if m["reasons"]:
            abstain.append({**out, "reasons": sorted(m["reasons"], key=REASONS.index), "votes": len(m["labelers"])})
        else:
            plain.append({**out, "votes": len(m["labelers"])})
    order = lambda x: (x["side"], x["pointer"] or "", x["start"] if x["start"] is not None else -1, x["role"])
    return sorted(abstain, key=order), sorted(plain, key=order)


def add_pointers(spans: list, state: str, state_format: str) -> None:
    """
    API_CONTRACT rule 7 (amended 2026-10-06): state-side offsets index into the
    canonical rendering; ``pointers`` gives the same evidence as RFC 6901
    pointers into the JSON state (null for text states, where offsets already
    index into the state as sent).
    """
    rendered = None
    for s in spans:
        s["pointers"] = None
        if s["side"] != "state" or s["start"] is None:
            continue
        rendered = rendered or render_state(state, state_format)
        if rendered.format == "json":
            s["pointers"] = [{k: e[k] for k in ("pointer", "start", "end")}
                             for e in range_to_evidence(rendered, s["start"], s["end"])]


def _majority(votes: int, n: int) -> Optional[bool]:
    if votes * 2 > n:
        return True
    if votes * 2 < n:
        return False
    return None  # tie (§5.5: ties stay null)


def adjudicated(conn: sqlite3.Connection, item_id: str) -> Optional[dict]:
    """The latest adjudication (FR-43): answerable, reasons, spans, version, adjudicator."""
    from .adjudication import as_dict, latest

    out = as_dict(conn, latest(conn, item_id))
    if out:
        out.pop("note")  # free text stays in the app
    return out


def training_rows(conn: sqlite3.Connection, filters: Filters, text_included: bool = True) -> list[dict]:
    """
    One row per item whose human, blind, first-pass labels reach the item's
    overlap target (FR-46). Gold items, gold probes, re-label passes and model
    labels never enter training targets.
    """
    records = [r for r in load_annotations(conn, Filters(**{**filters.__dict__, "include_models": False,
                                                            "include_skipped": False}))
               if r["labeler_kind"] == "human" and r["_relabel_of"] is None and r["_blind"]
               and not r["_gold_probe"] and r["skipped"] is None and r["task"] != "clauses"]
    by_item: dict = {}
    for r in records:
        by_item.setdefault(r["item_id"], []).append(r)
    if not by_item:
        return []

    item_sql, params = item_filter_sql(filters)
    rows = []
    for item_id in sorted(by_item):
        recs = by_item[item_id]
        item = conn.execute(f"SELECT * FROM items i WHERE i.item_id = ? AND {item_sql}",
                            (item_id, *params)).fetchone()
        if item is None:
            continue
        batch_ids = sorted({r["_batch_id"] for r in recs})
        batches = conn.execute(
            f"""SELECT b.*, COALESCE(bi.target, b.overlap_target) AS target FROM batches b
                JOIN batch_items bi ON bi.batch_id = b.id AND bi.item_id = ?
                WHERE b.id IN ({','.join('?' * len(batch_ids))})""", (item_id, *batch_ids)).fetchall()
        target = max((b["target"] for b in batches), default=2)
        labelers = sorted({r["labeler"] for r in recs})
        n = len(labelers)
        if n < target:
            continue

        reason_set = []
        for b in batches:
            for reason in json.loads(b["reason_set_json"]):
                if reason not in reason_set:
                    reason_set.append(reason)
        answerable_votes = sum(1 for r in recs if r["answerable"])
        soft, majority, ties = {}, [], []
        for reason in REASONS:
            asked = [r for r in recs if r["reasons"] and r["reasons"].get(reason) is not None]
            if not asked:
                soft[reason] = None
                continue
            votes = sum(1 for r in asked if r["reasons"][reason])
            soft[reason] = votes / len(asked)
            m = _majority(votes, len(asked))
            if m:
                majority.append(reason)
            elif m is None:
                ties.append(reason)
        abstain_evidence, plain_evidence = merge_spans(recs)
        add_pointers(abstain_evidence + plain_evidence, item["state"], item["state_format"])
        adjudication = adjudicated(conn, item_id)
        guideline_versions = sorted({b["guideline_version"] for b in batches if b["guideline_version"]})

        row = {
            "schema": TRAIN_SCHEMA,
            "id": item["row_id"], "qid": item["qid"], "source": item["source"], "split": item["split"],
            "heldout": None if item["heldout"] is None else bool(item["heldout"]),
            "permissions": item["permissions"], "source_license": item["source_license"],
            # Human labels are human-made, so they don't change a libre row's tier (FR-59)
            "label_provenance": "human",
            "text_included": text_included, "state_sha256": item["state_sha256"],
        }
        if text_included:
            row["state"] = item["state"]
            row["question"] = json.loads(item["question_json"])
        row["targets"] = {"mbnli": {
            "abstain": {
                "p": round(1 - answerable_votes / n, 6),
                "reasons": {k: (None if v is None else round(v, 6)) for k, v in soft.items()},
                "evidence": abstain_evidence,
            },
            "evidence": plain_evidence,
        }}
        row["human"] = {
            "n_labelers": n, "labelers": labelers, "answerable_votes": answerable_votes,
            "majority": {"answerable": _majority(answerable_votes, n), "reasons": majority, "tied_reasons": ties},
            "adjudicated": adjudication,
            "batch": recs[0]["batch"] if len(batches) == 1 else sorted(b["name"] for b in batches),
            "guideline_version": guideline_versions[0] if len(guideline_versions) == 1 else (guideline_versions or None),
            "reason_set": "all10" if reason_set == list(REASONS) else reason_set,
        }
        rows.append(row)
    return rows


# ============================================================================
# Clauses (docs/e13/CLAUSE_TASK.md)
# ============================================================================

CLAUSES_SCHEMA = "e13.clauses/1"


def _clause_texts(conn: sqlite3.Connection, item_ids) -> dict:
    """item_id -> (hypothesis, rendered premise, item row)."""
    from .clauses import hypothesis_of

    out = {}
    for item_id in sorted(set(item_ids)):
        it = conn.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()
        out[item_id] = (hypothesis_of(json.loads(it["question_json"])) or "",
                        render_state(it["state"], it["state_format"]).text, it)
    return out


def clause_rows(conn: sqlite3.Connection, filters: Filters, text_included: bool = True) -> list[dict]:
    """
    One row per (item, labeler, batch) of a clauses batch, latest version, not
    skipped. Offsets index the hypothesis and the premise's canonical rendering
    (rule 7); ``words`` are indices into ``text.split()`` (the E17 harness's
    word indexing), derived here and never stored.
    """
    from .clauses import hypothesis_word_evidence, hypothesis_word_tags, word_indices

    records = [r for r in load_annotations(conn, Filters(**{**filters.__dict__, "include_skipped": False}))
               if r["task"] == "clauses" and r["label"]]
    texts = _clause_texts(conn, [r["item_id"] for r in records])
    rows = []
    for r in records:
        hypothesis, premise, item = texts[r["item_id"]]
        clauses = [{**c, "words": word_indices(hypothesis, c["start"], c["end"]),
                    "evidence": [{**e, "words": word_indices(premise, e["start"], e["end"])} for e in c["evidence"]]}
                   for c in r["clauses"]]
        row = {
            "schema": CLAUSES_SCHEMA,
            "id": item["row_id"], "qid": item["qid"], "item_id": r["item_id"], "source": item["source"],
            "split": item["split"], "permissions": item["permissions"], "state_sha256": item["state_sha256"],
            "batch": r["batch"], "relabel_of": r["_relabel_of"], "labeler": r["labeler"],
            "labeler_kind": r["labeler_kind"], "version": r["version"], "gold_probe": r["_gold_probe"],
            "label": r["label"], "label_derived": r["label_derived"],
            "label_override": r["label"] != r["label_derived"],
            "clauses": clauses,
            "hypothesis_word_tags": hypothesis_word_tags(hypothesis, r["clauses"]),
            "hypothesis_word_evidence": hypothesis_word_evidence(hypothesis, premise, r["clauses"]),
            "completion": r["completion"], "note": r["note"], "policy_override": r["policy_override"],
            "timing": r["timing"], "created_at": r["created_at"],
        }
        if text_included:
            row["premise"] = premise
            row["hypothesis"] = hypothesis
        rows.append(row)
    return rows


def clause_agreement_document(conn: sqlite3.Connection, filters: Filters, n_boot: int = 1000,
                              seed: int = 0) -> Optional[dict]:
    from .clauses_agreement import clause_agreement

    records = [r for r in load_annotations(conn, filters) if r["task"] == "clauses"]
    if not records:
        return None
    texts = {k: v[:2] for k, v in _clause_texts(conn, [r["item_id"] for r in records]).items()}
    return clause_agreement(records, texts, n_boot=n_boot, seed=seed)


# ============================================================================
# Annotation history (docs/e13/ANNOTATOR.md §9): the process, not just the result
# ============================================================================

HISTORY_SCHEMA = "e13.history/1"
HEDGE_WORDS = ("i think", "maybe", "probably", "might", "could be", "not sure", "perhaps", "i guess", "kind of",
               "sort of", "unless", "or maybe")


def history_rows(conn: sqlite3.Connection, filters: Filters, text_included: bool = True) -> list[dict]:
    """
    One row per (item, labeler, batch) of a clauses batch with any activity: every
    annotation version, utterance, proposal and note, in order, and the signals
    that mark a hard item (versions, label flips, hedges, rejected proposals,
    agent questions, time spent).
    """
    from .clauses import stored_clauses

    item_sql, params = item_filter_sql(filters)
    sql = f"""SELECT DISTINCT a.item_id, a.labeler_id, a.batch_id FROM annotations a JOIN items i ON i.item_id = a.item_id
              JOIN batches b ON b.id = a.batch_id WHERE b.task_type = 'clauses' AND {item_sql}
              UNION SELECT DISTINCT u.item_id, u.labeler_id, u.batch_id FROM utterances u JOIN items i ON i.item_id = u.item_id
              JOIN batches b ON b.id = u.batch_id WHERE b.task_type = 'clauses' AND {item_sql}"""
    keys = conn.execute(sql, (*params, *params)).fetchall()
    batch_names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM batches")}
    if filters.batches:
        keys = [k for k in keys if batch_names.get(k["batch_id"]) in filters.batches]
    rows = []
    for k in sorted(keys, key=lambda k: (k["item_id"], k["labeler_id"], k["batch_id"] or 0)):
        item_id, lid, bid = k["item_id"], k["labeler_id"], k["batch_id"]
        lab = conn.execute("SELECT pseudonym, kind FROM labelers WHERE id = ?", (lid,)).fetchone()
        if not filters.include_models and lab["kind"] != "human":
            continue
        anns = conn.execute("SELECT * FROM annotations WHERE item_id = ? AND labeler_id = ? AND batch_id IS ? "
                            "ORDER BY version", (item_id, lid, bid)).fetchall()
        clauses = stored_clauses(conn, [a["id"] for a in anns])
        accepted = {p["accepted_annotation_id"]: p["id"] for p in conn.execute(
            "SELECT id, accepted_annotation_id FROM proposals WHERE accepted_annotation_id IS NOT NULL")}
        versions = [{"version": a["version"], "label": a["label"], "label_derived": a["label_derived"],
                     "skipped": a["skipped_code"], "clauses": [{"text": c["text"], "start": c["start"], "end": c["end"],
                                                                "stance": c["stance"], "omission": c["omission"],
                                                                "evidence": [e["text"] for e in c["evidence"]]}
                                                               for c in clauses.get(a["id"], [])],
                     "from_proposal": accepted.get(a["id"]), "active_ms": a["active_ms"],
                     "created_at": a["created_at"]} for a in anns]
        utts = [{"id": u["id"], "kind": u["kind"], "source": u["source"], "duration_ms": u["duration_ms"],
                 "text": u["text"] if text_included else None, "transcribed": u["text"] is not None,
                 "stt_engine": u["stt_engine"], "created_at": u["created_at"]}
                for u in conn.execute("SELECT * FROM utterances WHERE item_id = ? AND labeler_id = ? AND batch_id IS ? "
                                      "ORDER BY id", (item_id, lid, bid))]
        props = []
        for p in conn.execute("SELECT * FROM proposals WHERE item_id = ? AND labeler_id = ? AND batch_id IS ? "
                              "ORDER BY id", (item_id, lid, bid)):
            payload = json.loads(p["payload_json"] or "{}")
            props.append({"id": p["id"], "status": p["status"], "label": payload.get("label"),
                          "questions": payload.get("questions") or [], "problems": json.loads(p["problems_json"]),
                          "utterance_ids": json.loads(p["utterance_ids_json"]), "engine": p["engine"],
                          "created_at": p["created_at"], "reviewed_at": p["reviewed_at"]})
        notes = [{"category": n["category"], "text": n["text"] if text_included else None, "hedge": bool(n["hedge"]),
                  "nodes": json.loads(n["target_json"]).get("nodes", []), "source": n["source"],
                  "retracted": n["retracted_at"] is not None, "created_at": n["created_at"]}
                 for n in conn.execute("SELECT * FROM notes WHERE item_id = ? AND labeler_id = ? ORDER BY id",
                                       (item_id, lid))]
        labels = [v["label"] for v in versions if v["label"]]
        spoken = " ".join((u["text"] or "") for u in utts).lower()
        rows.append({
            "schema": HISTORY_SCHEMA, "item_id": item_id, "batch": batch_names.get(bid), "labeler": lab["pseudonym"],
            "labeler_kind": lab["kind"], "versions": versions, "utterances": utts, "proposals": props, "notes": notes,
            "signals": {
                "n_versions": len(versions), "label_flips": sum(1 for a, b in zip(labels, labels[1:]) if a != b),
                "final_label": labels[-1] if labels else None,
                "hedged_notes": sum(1 for n in notes if n["hedge"] and not n["retracted"]),
                "hedge_words_spoken": sum(spoken.count(w) for w in HEDGE_WORDS),
                "rejected_proposals": sum(1 for p in props if p["status"] == "rejected"),
                "agent_questions": sum(len(p["questions"]) for p in props),
                "utterances": len(utts), "audio_ms": sum(u["duration_ms"] or 0 for u in utts),
                "active_ms": sum(v["active_ms"] or 0 for v in versions),
            },
        })
    return rows


# ============================================================================
# FR-5 items (pool import format)
# ============================================================================

def item_rows(conn: sqlite3.Connection, filters: Filters) -> list[dict]:
    """
    The items in the E09 pool import format (§5.2), one row per row_id, with the
    stored state byte for byte, so an export re-imports to identical states and
    hashes (FR-5). Batch filters select rows with any item in those batches.
    """
    item_sql, params = item_filter_sql(filters)
    sql = f"SELECT i.* FROM items i WHERE {item_sql}"
    if filters.batches:
        sql += f""" AND i.item_id IN (SELECT bi.item_id FROM batch_items bi JOIN batches b ON b.id = bi.batch_id
                                      WHERE b.name IN ({','.join('?' * len(filters.batches))}))"""
        params += list(filters.batches)
    rows: dict = {}
    for i in conn.execute(sql + " ORDER BY i.row_id, i.rowid", params):
        row = rows.setdefault(i["row_id"], {
            "id": i["row_id"], "source": i["source"], "split": i["split"],
            "heldout": None if i["heldout"] is None else bool(i["heldout"]),
            "state": i["state"], "state_format": i["state_format"],
            "questions": {}, "gold": {}, "permissions": i["permissions"],
            "_e13": {}, "_candidate_for": {}, "_answers": {},
        })
        row["questions"][i["qid"]] = json.loads(i["question_json"])
        if i["gold_json"] is not None:
            row["gold"][i["qid"]] = json.loads(i["gold_json"])
        e13 = json.loads(i["e13_json"]) if i["e13_json"] else {}
        if "candidate_for" in e13:
            row["_candidate_for"][i["qid"]] = e13.pop("candidate_for")
        row["_e13"].update(e13)
        for teacher, answer in (json.loads(i["model_answers_json"]) if i["model_answers_json"] else {}).items():
            row["_answers"].setdefault(teacher, {})[i["qid"]] = answer
    out = []
    for row in rows.values():
        e13 = row.pop("_e13")
        candidate_for = row.pop("_candidate_for")
        answers = row.pop("_answers")
        if candidate_for:
            e13["candidate_for"] = candidate_for
        if e13:
            row["e13"] = e13
        if answers:
            row["model_answers"] = answers
        out.append(row)
    return out


# ============================================================================
# FR-48 agreement
# ============================================================================

def agreement_document(conn: sqlite3.Connection, filters: Filters, n_boot: int = 1000, seed: int = 0) -> dict:
    from .records import agreement_inputs

    data = agreement_inputs(conn, filters)
    doc = report(data, n_boot=n_boot, seed=seed)
    doc["filters"] = filters.as_dict()
    clauses = clause_agreement_document(conn, filters, n_boot=n_boot, seed=seed)
    if clauses is not None:
        doc["clauses"] = clauses  # from the clauses export's rows, not from doc["data"]
    # The exact inputs, so `analysis.report(doc["data"], n_boot=..., seed=...)` reproduces every number
    doc["data"] = data
    return doc


# ============================================================================
# FR-49 the export run
# ============================================================================

def db_checksum(conn: sqlite3.Connection) -> str:
    """sha256 of a consistent snapshot of the database (WAL included)."""
    snapshot = sqlite3.connect(":memory:")
    try:
        conn.backup(snapshot)
        return "sha256:" + hashlib.sha256(snapshot.serialize()).hexdigest()
    finally:
        snapshot.close()


def _new_dir(root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path, n = root / stamp, 1
    while path.exists():  # never overwrite an earlier export
        n += 1
        path = root / f"{stamp}-{n}"
    path.mkdir(parents=True)
    return path


def _write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(_dumps(row) + "\n")
            n += 1
    return n


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def default_kinds(conn: sqlite3.Connection) -> tuple:
    """Every kind; clauses and history only when a clauses batch exists."""
    has_clauses = conn.execute("SELECT 1 FROM batches WHERE task_type = 'clauses' LIMIT 1").fetchone()
    return tuple(k for k in KINDS if k not in ("clauses", "history") or has_clauses)


def write_export(conn: sqlite3.Connection, kinds: Optional[Sequence[str]] = None, filters: Filters = Filters(),
                 out_root: Optional[Path] = None, text_included: bool = True, actor_id=None,
                 n_boot: int = 1000, seed: int = 0) -> dict:
    """Write the requested exports (default: default_kinds) into a fresh timestamped directory. Returns the manifest."""
    kinds = default_kinds(conn) if kinds is None else kinds
    unknown = set(kinds) - set(KINDS)
    if unknown:
        raise ValueError(f"unknown export kind(s): {', '.join(sorted(unknown))}")
    filters.validate()
    checksum = db_checksum(conn)  # before anything (the audit row) changes the database
    out = _new_dir(Path(out_root) if out_root else config.OUTPUTS_DIR / "exports")
    files = []
    if "annotations" in kinds:
        files.append(("annotations.jsonl", _write_jsonl(out / "annotations.jsonl", annotation_rows(conn, filters))))
    if "training" in kinds:
        files.append(("training.jsonl", _write_jsonl(out / "training.jsonl",
                                                     training_rows(conn, filters, text_included))))
    if "clauses" in kinds:
        files.append(("clauses.jsonl", _write_jsonl(out / "clauses.jsonl", clause_rows(conn, filters, text_included))))
    if "history" in kinds:
        files.append(("history.jsonl", _write_jsonl(out / "history.jsonl", history_rows(conn, filters, text_included))))
    if "items" in kinds:
        if not text_included:
            raise ValueError("the items export carries state text; it can't be written with text_included off")
        files.append(("items.jsonl", _write_jsonl(out / "items.jsonl", item_rows(conn, filters))))
    if "agreement" in kinds:
        doc = agreement_document(conn, filters, n_boot=n_boot, seed=seed)
        (out / "agreement.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        files.append(("agreement.json", len(doc["data"])))

    batch_names = list(filters.batches) or [r[0] for r in conn.execute("SELECT name FROM batches")]
    guideline_versions = sorted({r[0] for r in conn.execute(
        f"SELECT guideline_version FROM batches WHERE name IN ({','.join('?' * len(batch_names))})"
        " AND guideline_version IS NOT NULL", batch_names)}) if batch_names else []
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "app_version": config.app_version(),
        "guideline_versions": guideline_versions,
        "db_sha256": checksum,
        "filters": filters.as_dict(),
        "text_included": text_included,
        "files": [{"name": name, "rows": n, "sha256": _sha256(out / name)} for name, n in files],
        "directory": str(out),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    audit(conn, actor_id, "export", str(out), {"files": [f["name"] for f in manifest["files"]],
                                               "filters": manifest["filters"]})
    return manifest
