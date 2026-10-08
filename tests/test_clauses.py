"""
The `clauses` task (docs/e13/CLAUSE_TASK.md): hypothesis clauses with a stance
and linked premise evidence; the sentence label is derived.
"""
import json

import pytest
from fastapi.testclient import TestClient

from e13_labeler.clauses import (Clause, ClauseSubmission, Evidence, derive_label, hypothesis_word_evidence,
                                 hypothesis_word_tags, validate_clauses, word_indices)
from e13_labeler.labelling import PolicyViolation, SubmissionError
from e13_labeler.render_state import RENDERER

PREMISE = "A dog looks up at its loving owner on the beach."
HYP = "A brown dog is looking up at a man."
Q = {"type": "choice", "instructions": "Split the hypothesis into clauses.",
     "criteria": {"entailment": "true", "neutral": "open", "contradiction": "false"}, "hypothesis": HYP}


def span(text, sub, nth=0):
    i = -1
    for _ in range(nth + 1):
        i = text.index(sub, i + 1)
    return i, i + len(sub)


def clause(sub, stance, *evidence, omission=False):
    s, e = span(HYP, sub)
    return Clause(s, e, sub, stance, omission=omission,
                  evidence=[Evidence(*span(PREMISE, ev), ev) for ev in evidence])


def csub(*clauses, override=None, completion=None, note=None, policy=False):
    return ClauseSubmission(list(clauses), override, completion, note, policy, None)


def check(sub):
    validate_clauses(sub, state=PREMISE, state_format="text", question=Q)
    return sub


# ============================================================================
# Derivation and validation
# ============================================================================

class TestDerivation:
    def test_rule(self):
        assert derive_label(["supported", "supported"]) == "entailment"
        assert derive_label(["supported", "contradicted", "unaddressed"]) == "contradiction"
        assert derive_label(["supported", "undetermined"]) == "neutral"
        assert derive_label(["supported", "unaddressed"]) == "neutral"
        assert derive_label([]) == "neutral"

    def test_derived_label_is_stored(self):
        s = check(csub(clause("dog", "supported", "dog"), clause("brown", "undetermined", "dog"),
                       clause("looking up", "supported", "looks up"), clause("a man", "undetermined", "owner")))
        assert (s.label_derived, s.label) == ("neutral", "neutral")
        assert [c.text for c in s.clauses] == ["brown", "dog", "looking up", "a man"]   # hypothesis order
        assert all(ev.renderer == RENDERER for c in s.clauses for ev in c.evidence)

    def test_contradiction_wins(self):
        s = check(csub(clause("dog", "supported", "dog"), clause("brown", "unaddressed"),
                       clause("a man", "contradicted", "owner")))
        assert s.label == "contradiction"


class TestValidation:
    def problems(self, sub):
        with pytest.raises(SubmissionError) as e:
            check(sub)
        return e.value.problems

    def test_needs_a_clause(self):
        assert "mark at least one clause" in self.problems(csub())[0]

    def test_slices_and_overlap(self):
        bad = clause("dog", "supported", "dog")
        bad.text = "cat"
        assert "is not the slice" in self.problems(csub(bad))[0]
        s, e = span(HYP, "brown dog")
        assert "overlaps" in " ".join(self.problems(csub(clause("dog", "supported", "dog"),
                                                         Clause(s, e, "brown dog", "undetermined"))))
        s, e = span(HYP, "dog ")
        assert "non-space" in self.problems(csub(Clause(s, e, "dog ", "unaddressed")))[0]

    def test_stance_rules(self):
        assert "stance must be" in self.problems(csub(clause("dog", "entailed")))[0]
        assert "omission is for contradicted" in self.problems(csub(clause("dog", "supported", "dog",
                                                                           omission=True)))[0]
        assert "unaddressed clause has no premise evidence" in self.problems(csub(clause("dog", "unaddressed",
                                                                                         "dog")))[0]
        dup = clause("dog", "supported", "dog")
        dup.evidence.append(Evidence(*span(PREMISE, "dog"), "dog"))
        assert "duplicate" in self.problems(csub(dup))[0]

    def test_missing_evidence_is_overridable(self):
        with pytest.raises(PolicyViolation) as e:
            check(csub(clause("dog", "supported")))
        assert "no premise evidence" in e.value.problems[0]
        s = check(csub(clause("dog", "supported"), policy=True))
        assert s.policy_override and s.label == "entailment"
        assert not check(csub(clause("dog", "supported", "dog"), policy=True)).policy_override  # nothing overridden

    def test_override_needs_a_note(self):
        assert "needs a note" in self.problems(csub(clause("dog", "supported", "dog"), override="neutral"))[0]
        s = check(csub(clause("dog", "supported", "dog"), override="neutral", note="disjunction"))
        assert (s.label_derived, s.label, s.label_override) == ("entailment", "neutral", "neutral")
        s = check(csub(clause("dog", "supported", "dog"), override="entailment"))
        assert s.label_override is None   # same as derived: not an override

    def test_completion(self):
        assert "neutral items only" in self.problems(csub(clause("dog", "supported", "dog"),
                                                          completion={"entail": "x"}))[0]
        s = check(csub(clause("a man", "unaddressed"), completion={"entail": " The owner is a man. ",
                                                                   "contradict": ""}))
        assert s.completion == {"entail": "The owner is a man.", "contradict": None}
        with pytest.raises(PolicyViolation):
            check(csub(clause("a man", "unaddressed"), completion={"entail": " ".join(["word"] * 13)}))

    def test_item_needs_a_hypothesis(self):
        with pytest.raises(SubmissionError):
            validate_clauses(csub(clause("dog", "unaddressed")), state=PREMISE, state_format="text",
                             question={k: v for k, v in Q.items() if k != "hypothesis"})


class TestWords:
    def test_split_indices(self):
        assert word_indices(HYP, *span(HYP, "looking up")) == [4, 5]
        assert word_indices(PREMISE, *span(PREMISE, "beach")) == [10]         # "beach." is word 10
        cl = [{"start": a, "end": b, "stance": st, "evidence": [{"start": x, "end": y} for x, y in ev]}
              for (a, b), st, ev in [(span(HYP, "dog"), "supported", [span(PREMISE, "dog")]),
                                     (span(HYP, "a man."), "undetermined", [span(PREMISE, "owner")])]]
        assert hypothesis_word_tags(HYP, cl) == [None, None, "supported", None, None, None, None,
                                                 "undetermined", "undetermined"]
        ev = hypothesis_word_evidence(HYP, PREMISE, cl)
        assert ev[2] == [1] and ev[7] == ev[8] == [7] and ev[0] == []


# ============================================================================
# API, storage, export, agreement
# ============================================================================

ROWS = [
    {"id": f"pair{i}", "source": "snli", "split": "test", "state": PREMISE, "state_format": "text",
     "questions": {"clauses": Q}} for i in range(3)
]


@pytest.fixture
def clause_batch(db, tmp_path):
    from e13_labeler.batches import set_status
    from e13_labeler.db import get_db
    from e13_labeler.importer import import_file

    path = tmp_path / "pairs.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in ROWS))
    with get_db() as conn:
        report = import_file(conn, path, batch="nli-clauses", task_type="clauses")
        assert report.n_items == 3 and not report.errors
        set_status(conn, "nli-clauses", "open")


def body(item_id, *clauses, **extra):
    return {"item_id": item_id, "clauses": [
        {"start": c.start, "end": c.end, "text": c.text, "stance": c.stance, "omission": c.omission,
         "evidence": [{"start": e.start, "end": e.end, "text": e.text} for e in c.evidence]} for c in clauses],
        **extra}


class TestBatchesAndImport:
    def test_clause_batch_has_no_reasons(self, clause_batch):
        from e13_labeler.db import get_db

        with get_db() as conn:
            b = conn.execute("SELECT * FROM batches WHERE name = 'nli-clauses'").fetchone()
        assert (b["task_type"], json.loads(b["reason_set_json"]), json.loads(b["span_policy_json"])) == \
            ("clauses", [], {})

    def test_task_type_must_match(self, clause_batch, tmp_path):
        from e13_labeler.db import get_db
        from e13_labeler.importer import import_file

        path = tmp_path / "x.jsonl"
        path.write_text(json.dumps(ROWS[0]) + "\n")
        with get_db() as conn, pytest.raises(ValueError, match="is a clauses batch"):
            import_file(conn, path, batch="nli-clauses", task_type="reasons")

    def test_rows_without_hypothesis_are_rejected(self, db, tmp_path):
        from e13_labeler.db import get_db
        from e13_labeler.importer import import_file

        row = {**ROWS[0], "questions": {"q": {k: v for k, v in Q.items() if k != "hypothesis"}}}
        path = tmp_path / "x.jsonl"
        path.write_text(json.dumps(row) + "\n")
        with get_db() as conn:
            report = import_file(conn, path, batch="c2", task_type="clauses")
        assert report.n_rejected == 1 and "needs question.hypothesis" in report.errors[0][1]

    def test_set_reasons(self, db):
        from e13_labeler.batches import ensure_batch, set_reasons, set_status
        from e13_labeler.db import get_db

        with get_db() as conn:
            ensure_batch(conn, "r")
            out = set_reasons(conn, "r", ["subjective", "not_enough_info"])
            assert out["reason_set"] == ["not_enough_info", "subjective"]   # canonical order
            set_status(conn, "r", "open")
            with pytest.raises(ValueError, match="is open"):
                set_reasons(conn, "r", ["unrelated"])
            assert set_reasons(conn, "r", ["unrelated"], force=True)["reason_set"] == ["unrelated"]
            with pytest.raises(ValueError):
                set_reasons(conn, "r", ["nope"])


class TestApi:
    def test_payload_carries_the_hypothesis(self, clause_batch, owner_client: TestClient):
        item = owner_client.get("/api/next").json()
        assert item["task_type"] == "clauses" and item["question"]["hypothesis"] == HYP
        assert item["reason_set"] == [] and item["state_rendered"] == PREMISE

    def test_submit_store_edit(self, clause_batch, owner_client: TestClient):
        from e13_labeler.db import get_db

        item = owner_client.get("/api/next").json()
        r = owner_client.post("/api/annotations", json=body(
            item["item_id"], clause("dog", "supported", "dog"), clause("looking up", "supported", "looks up"),
            clause("a man", "undetermined", "owner"), completion={"entail": "The owner is a man."},
            note="owner's sex unknown"))
        assert r.status_code == 200, r.text
        aid = r.json()["annotation_id"]
        with get_db() as conn:
            a = conn.execute("SELECT * FROM annotations WHERE id = ?", (aid,)).fetchone()
            assert (a["label"], a["label_derived"], a["answerable"], a["reasons_json"]) == \
                ("neutral", "neutral", None, None)
            cl = conn.execute("SELECT * FROM clauses WHERE annotation_id = ? ORDER BY idx", (aid,)).fetchall()
            assert [c["text"] for c in cl] == ["dog", "looking up", "a man"]
            ev = conn.execute("SELECT * FROM clause_evidence WHERE clause_id = ?", (cl[2]["id"],)).fetchall()
            assert [(e["text"], e["renderer"]) for e in ev] == [("owner", RENDERER)]

        hist = owner_client.get("/api/history").json()["submissions"]
        assert hist[0]["label"] == "neutral"
        edit = owner_client.get(f"/api/history/{aid}").json()["edit"]
        assert [c["stance"] for c in edit["clauses"]] == ["supported", "supported", "undetermined"]
        assert edit["completion"] == {"entail": "The owner is a man.", "contradict": None}
        assert edit["label_override"] is None
        r = owner_client.put(f"/api/annotations/{aid}", json=body(
            item["item_id"], clause("dog", "supported", "dog"), clause("a man", "contradicted", "owner"),
            label_override="neutral", note="owner could be the man's"))
        assert r.status_code == 200 and r.json()["version"] == 2, r.text
        with get_db() as conn:
            a = conn.execute("SELECT * FROM annotations WHERE id = ?", (r.json()["annotation_id"],)).fetchone()
            assert (a["label"], a["label_derived"]) == ("neutral", "contradiction")

    def test_errors(self, clause_batch, owner_client: TestClient):
        item = owner_client.get("/api/next").json()
        r = owner_client.post("/api/annotations", json={"item_id": item["item_id"], "answerable": True})
        assert r.status_code == 422
        r = owner_client.post("/api/annotations", json=body(item["item_id"], clause("dog", "supported")))
        assert r.status_code == 422 and r.json()["detail"]["policy"] is True
        r = owner_client.post("/api/annotations", json=body(item["item_id"], clause("dog", "supported"),
                                                            policy_override=True))
        assert r.status_code == 200 and r.json()["policy_override"] is True

    def test_no_reasons_gold_probes_in_clause_batches(self, clause_batch):
        from e13_labeler.app import _probe_candidate
        from e13_labeler.db import get_db

        with get_db() as conn:
            b = conn.execute("SELECT * FROM batches WHERE name = 'nli-clauses'").fetchone()
            assert _probe_candidate(conn, b, {"role": "labeler", "clearance": "internal", "id": 1}) is None


class TestExportAndAgreement:
    def label_all(self, client, stances):
        for _ in ROWS:
            item = client.get("/api/next").json()
            clauses = [clause("dog", "supported", "dog"), clause("a man", stances[item["item_id"]],
                                                                  *([] if stances[item["item_id"]] == "unaddressed"
                                                                    else ["owner"]))]
            assert client.post("/api/annotations", json=body(item["item_id"], *clauses)).status_code == 200

    def test_export_rows_and_agreement(self, clause_batch, owner_client: TestClient, fresh_client, tmp_path):
        from e13_labeler.db import get_db
        from e13_labeler.exports import write_export
        from e13_labeler.records import Filters
        from tests.conftest import login, make_labeler

        self.label_all(owner_client, {"pair0#clauses": "undetermined", "pair1#clauses": "contradicted",
                                      "pair2#clauses": "unaddressed"})
        owner_client.post("/api/auth/logout")
        make_labeler("two", clearance="internal")
        login(fresh_client, "two")
        self.label_all(fresh_client, {"pair0#clauses": "undetermined", "pair1#clauses": "undetermined",
                                      "pair2#clauses": "unaddressed"})
        with get_db() as conn:
            m = write_export(conn, filters=Filters(), out_root=tmp_path / "ex", n_boot=50)
        names = {f["name"] for f in m["files"]}
        assert "clauses.jsonl" in names
        out = tmp_path / "ex" / m["directory"].split("/")[-1]
        rows = [json.loads(line) for line in (out / "clauses.jsonl").read_text().splitlines()]
        assert len(rows) == 6
        r = next(r for r in rows if r["item_id"] == "pair1#clauses" and r["label"] == "contradiction")
        assert r["hypothesis"] == HYP and r["premise"] == PREMISE
        assert r["clauses"][0]["words"] == [2] and r["clauses"][0]["evidence"][0]["words"] == [1]
        assert r["hypothesis_word_tags"][2] == "supported" and r["hypothesis_word_tags"][7] == "contradicted"
        assert not r["label_override"]
        # annotations export carries the clauses; training rows (reasons targets) never do
        ann = [json.loads(line) for line in (out / "annotations.jsonl").read_text().splitlines()]
        assert {a["task"] for a in ann} == {"clauses"} and all(a["clauses"] for a in ann)
        assert (out / "training.jsonl").read_text() == ""
        doc = json.loads((out / "agreement.json").read_text())
        c = doc["clauses"]
        assert c["inter_rater"]["n_items"] == 3 and c["n_annotations"] == 6
        assert c["label_counts"] == {"contradiction": 1, "neutral": 5}
        assert c["inter_rater"]["evidence_f1"]["mean"] == 1.0          # same premise words wherever both linked
        assert c["inter_rater"]["word_stance"]["alpha"] < 1.0           # pair1 "a man" differs
        assert doc["inter_rater"]["n_items_pairable"] == 0              # reasons α never sees clause labels
