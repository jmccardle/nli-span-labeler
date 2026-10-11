"""
Annotator mode (docs/e13/ANNOTATOR.md): curated batches, numbered parses,
utterances through the job queue to agent proposals, review, notes.

The engines are fakes (local HTTP servers speaking the OpenAI-compatible
shapes), so no model runs.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

PREMISE = "A man is standing in the doorway of a building."
HYP = "The man is walking into a room."
Q = {"type": "choice", "instructions": "Clauses.", "criteria": {"entailment": "t", "neutral": "o", "contradiction": "f"},
     "hypothesis": HYP}
ROWS = [{"id": f"p{i}", "source": "snli", "split": "test", "state": PREMISE, "state_format": "text",
         "questions": {"clauses": Q}} for i in range(2)]
ITEM = "p0#clauses"
# premise nodes 1-11 (A man is standing in the doorway of a building .), hypothesis 12-19
CLAUSE_MAN = {"span": {"nodes": [13], "phrase": False}, "stance": "supported",
              "evidence": [{"nodes": [2], "phrase": False}], "omission": False}
CLAUSE_WALKING = {"span": {"nodes": [15], "phrase": False}, "stance": "contradicted",
                  "evidence": [{"nodes": [4], "phrase": False}], "omission": False}
CLAUSE_ROOM = {"span": {"nodes": [16], "phrase": True}, "stance": "undetermined",
               "evidence": [{"nodes": [5], "phrase": True}], "omission": False}
REFERENT = {"from": {"nodes": [2], "phrase": False}, "to": {"nodes": [13], "phrase": False}, "type": "referent",
            "note": None}


def edits(**kw):
    """An agent answer: edits to the current annotation (nothing by default)."""
    out = {"add_clauses": [], "change_clauses": [], "remove_clauses": [], "add_relations": [], "remove_relations": [],
           "notes": [], "label_override": None, "completion": None, "questions": []}
    return {**out, **kw}


AGENT_ANSWER = edits(
    add_clauses=[CLAUSE_MAN, CLAUSE_WALKING, CLAUSE_ROOM], add_relations=[REFERENT],
    notes=[{"nodes": [4, 15], "category": "lexical", "text": "standing and walking can't both hold now", "hedge": False},
           {"nodes": [16], "category": "relation", "text": "a doorway might lead into a room, I think", "hedge": True}])


class Fake:
    """A local engine: answers POSTs with the next canned response and records the requests."""

    def __init__(self, responses):
        self.responses, self.requests = list(responses), []
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                fake.requests.append({"path": self.path, "ctype": self.headers.get("content-type"), "body": body,
                                      "auth": self.headers.get("authorization")})
                nxt = fake.responses.pop(0) if fake.responses else {}
                status, nxt = nxt if isinstance(nxt, tuple) else (200, nxt)
                out = json.dumps(nxt).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def chat_reply(obj):
    return {"choices": [{"message": {"content": json.dumps(obj)}}]}


@pytest.fixture
def curated(db, tmp_path, monkeypatch):
    from e13_labeler.batches import configure, set_status
    from e13_labeler.db import get_db
    from e13_labeler.importer import import_file

    monkeypatch.setenv("E13_OUTPUTS", str(tmp_path / "outputs"))
    from e13_labeler import config
    monkeypatch.setattr(config, "OUTPUTS_DIR", tmp_path / "outputs")
    monkeypatch.delenv("E13_STT_URL", raising=False)
    monkeypatch.delenv("E13_AGENT_URL", raising=False)
    path = tmp_path / "pairs.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in ROWS))
    with get_db() as conn:
        import_file(conn, path, batch="cur", task_type="clauses")
        configure(conn, "cur", mode="curated", overlap_target=1)
        set_status(conn, "cur", "open")


def run_jobs():
    from e13_labeler.annotator import run_one
    from e13_labeler.db import get_db

    out = []
    while True:
        with get_db() as conn:   # make waiting jobs runnable now
            conn.execute("UPDATE jobs SET next_try_at = NULL")
        r = run_one(get_db, "test")
        if r is None or r["status"] == "waiting" or len(out) > 10:
            if r:
                out.append(r)
            return out
        out.append(r)


def ws(client, item=ITEM):
    r = client.get(f"/api/annotator/items/{item.replace('#', '%23')}/workspace", params={"batch": "cur"})
    assert r.status_code == 200, r.text
    return r.json()


def url(item, tail):
    return f"/api/annotator/items/{item.replace('#', '%23')}/{tail}"


class TestParse:
    def test_numbering_and_phrases(self):
        from e13_labeler.parse import build, resolve

        n = build(PREMISE, "text", Q)
        assert [x["n"] for x in n["premise"]] == list(range(1, 12))
        assert [x["text"] for x in n["hypothesis"]][:4] == ["The", "man", "is", "walking"]
        assert n["hypothesis"][0]["n"] == 12
        into = resolve(n, [16])                      # "into a room" (the phrase), not the full stop
        assert HYP[into["start"]:into["end"]] == "into a room" and into["side"] == "hypothesis"
        assert HYP[slice(*[resolve(n, [16], phrase=False)[k] for k in ("start", "end")])] == "into"
        assert resolve(n, [2, 13]) is None           # both sides
        assert resolve(n, [99]) is None

    def test_json_premise_numbers_fields(self):
        from e13_labeler.parse import build

        n = build('{"b": {"c": 1}, "a": "two words"}', "json", Q)
        assert [(x["n"], x["text"], x["head_n"]) for x in n["premise"]] == [(1, "a", None), (2, "b", None),
                                                                            (3, "c", 2)]
        assert n["hypothesis"][0]["n"] == 4


class TestCurated:
    def test_curated_batches_are_not_served_by_the_queue(self, curated, owner_client: TestClient):
        assert owner_client.get("/api/next").status_code == 404
        b = owner_client.get("/api/annotator/batches").json()["batches"]
        assert b == [{"name": "cur", "task_type": "clauses", "n_items": 2, "n_labelled": 0}]

    def test_only_clauses_batches_can_be_curated(self, db):
        from e13_labeler.batches import configure, ensure_batch
        from e13_labeler.db import get_db

        with get_db() as conn:
            ensure_batch(conn, "r")
            with pytest.raises(ValueError, match="only clauses"):
                configure(conn, "r", mode="curated")

    def test_workspace_and_manual_versions(self, curated, owner_client: TestClient):
        w = ws(owner_client)
        assert w["parse"]["nodes"]["hypothesis"][0]["n"] == 12 and w["annotation"] is None
        clause = {"start": HYP.index("walking"), "end": HYP.index("walking") + 7, "text": "walking",
                  "stance": "contradicted",
                  "evidence": [{"start": PREMISE.index("standing"), "end": PREMISE.index("standing") + 8,
                                "text": "standing"}]}
        for k in range(2):
            r = owner_client.post(url(ITEM, "annotations"), params={"batch": "cur"},
                                  json={"item_id": ITEM, "clauses": [clause]})
            assert r.status_code == 200 and r.json()["version"] == k + 1, r.text
        w = ws(owner_client)
        assert w["annotation"]["label"] == "contradiction" and len(w["versions"]) == 2
        assert w["annotation"]["clauses"][0]["nodes"] == [15]
        assert w["annotation"]["clauses"][0]["evidence"][0]["nodes"] == [4]
        items = owner_client.get("/api/annotator/batches/cur/items").json()["items"]
        assert items[0]["label"] == "contradiction" and items[0]["versions"] == 2


class TestPipeline:
    def test_typed_utterance_waits_then_proposes_and_is_accepted(self, curated, owner_client, monkeypatch):
        r = owner_client.post(url(ITEM, "utterances/text"), params={"batch": "cur"},
                              json={"text": "13 is supported by 2. 15 contradicts 4. 16 is undetermined, 5 suggests it."})
        assert r.status_code == 200
        assert run_jobs()[0]["status"] == "waiting"            # no agent configured: the job waits
        st = owner_client.get("/api/annotator/queue").json()
        assert st["counts"]["agent"] == {"waiting": 1} and "E13_AGENT_URL" in st["waiting_on"]["error"]
        items = owner_client.get("/api/annotator/batches/cur/items").json()["items"]
        assert items[0]["jobs"] == {"waiting": 1} and items[0]["utterances"] == 1

        agent = Fake([chat_reply(AGENT_ANSWER)])
        monkeypatch.setenv("E13_AGENT_URL", agent.url + "/v1")
        try:
            done = run_jobs()
        finally:
            agent.close()
        assert done[0]["status"] == "done" and done[0]["problems"] == 0, done
        sent = json.loads(agent.requests[0]["body"])
        assert agent.requests[0]["path"] == "/v1/chat/completions"
        assert sent["response_format"]["type"] == "json_schema"
        prompt = sent["messages"][1]["content"]
        assert "walking[15]" in prompt and "(NEW, #" in prompt and "13 is supported by 2" in prompt
        assert "entailment" not in prompt.split("WHAT THE ANNOTATOR SAID")[0].split("HYPOTHESIS:")[0]  # no gold

        p = ws(owner_client)["proposal"]
        assert [c["text"] for c in p["payload"]["preview"]["clauses"]] == ["man", "walking", "into a room"]
        assert len(p["payload"]["delta"]["add_clauses"]) == 3
        assert p["payload"]["label"] == "contradiction" and p["problems"] == []
        r = owner_client.post(f"/api/annotator/proposals/{p['id']}/accept", json={})
        assert r.status_code == 200 and r.json()["version"] == 1, r.text
        w = ws(owner_client)
        assert w["annotation"]["label"] == "contradiction" and w["proposal"] is None
        assert w["annotation"]["relations"][0]["type"] == "referent"
        assert [(n["category"], n["hedge"]) for n in w["notes"]] == [("lexical", False), ("relation", True)]
        assert owner_client.post(f"/api/annotator/proposals/{p['id']}/accept", json={}).status_code == 409

    def test_audio_is_transcribed_then_proposed(self, curated, owner_client, monkeypatch):
        stt = Fake([{"text": " 13 is supported by 2. ", "segments": [{"start": 0.0, "end": 2.1, "text": "13 is…"}]}])
        agent = Fake([chat_reply(edits(add_clauses=[CLAUSE_MAN]))])
        monkeypatch.setenv("E13_STT_URL", stt.url + "/v1/audio/transcriptions")
        monkeypatch.setenv("E13_AGENT_URL", agent.url + "/v1")
        r = owner_client.post(url(ITEM, "utterances/audio"), params={"batch": "cur", "duration_ms": 2100},
                              content=b"\x1aE\xdf\xa3 fake webm", headers={"content-type": "audio/webm;codecs=opus"})
        assert r.status_code == 200, r.text
        try:
            done = run_jobs()
        finally:
            stt.close()
            agent.close()
        assert [d["kind"] for d in done] == ["transcribe", "agent"]
        assert b'name="file"' in stt.requests[0]["body"] and b"fake webm" in stt.requests[0]["body"]
        w = ws(owner_client)
        assert w["utterances"][0]["text"] == "13 is supported by 2." and w["utterances"][0]["audio"]
        assert w["proposal"]["payload"]["label"] == "entailment"
        audio = owner_client.get(f"/api/annotator/utterances/{w['utterances'][0]['id']}/audio")
        assert audio.status_code == 200 and audio.content.endswith(b"fake webm")

    def test_cloud_engines_keys_models_and_rate_limits(self, curated, owner_client, monkeypatch):
        """Mistral-style setup: keys by env-var name, model form fields, 429 waits, list-of-chunks content."""
        from e13_labeler.db import get_db

        stt = Fake([(429, {"message": "rate limited"}),
                    {"model": "voxtral-mini-latest", "text": "13 is supported by 2.",
                     "segments": [{"type": "transcription_segment", "text": "13 is supported by 2.", "start": 0.1,
                                   "end": 1.7}]}])
        chunks = {"choices": [{"message": {"content": [
            {"type": "thinking", "thinking": [{"type": "text", "text": "the annotator said 13…"}]},
            {"type": "text", "text": json.dumps(edits(add_clauses=[CLAUSE_MAN]))}]}}]}
        agent = Fake([chunks])
        monkeypatch.setenv("TEST_CLOUD_KEY", "sekrit-123")
        monkeypatch.setenv("E13_STT_URL", stt.url + "/v1/audio/transcriptions")
        monkeypatch.setenv("E13_STT_MODEL", "voxtral-mini-latest")
        monkeypatch.setenv("E13_STT_TIMESTAMPS", "segment")
        monkeypatch.setenv("E13_STT_KEY_ENV", "TEST_CLOUD_KEY")
        monkeypatch.setenv("E13_AGENT_URL", agent.url + "/v1")
        monkeypatch.setenv("E13_AGENT_MODEL", "mistral-small-2603")
        monkeypatch.setenv("E13_AGENT_KEY_ENV", "TEST_CLOUD_KEY")
        owner_client.post(url(ITEM, "utterances/audio"), params={"batch": "cur"}, content=b"webm bytes",
                          headers={"content-type": "audio/webm;codecs=opus"})
        try:
            first = run_jobs()
            assert first[0]["status"] == "waiting" and "429" in first[0]["error"]      # rate limit: wait, no failure
            done = run_jobs()
        finally:
            stt.close()
            agent.close()
        assert [d["kind"] for d in done] == ["transcribe", "agent"], done
        body = stt.requests[1]["body"]
        assert b'name="model"\r\n\r\nvoxtral-mini-latest' in body and b'name="timestamp_granularities"' in body
        assert b"Content-Type: audio/webm\r\n" in body                                 # codec parameter dropped
        assert stt.requests[1]["auth"] == agent.requests[0]["auth"] == "Bearer sekrit-123"
        sent = json.loads(agent.requests[0]["body"])
        assert sent["model"] == "mistral-small-2603" and sent["response_format"]["json_schema"]["strict"] is True
        assert ws(owner_client)["proposal"]["payload"]["label"] == "entailment"
        with get_db() as conn:
            stored = " ".join(str(tuple(r)) for t in ("jobs", "utterances", "proposals")
                              for r in conn.execute(f"SELECT * FROM {t}"))
        assert "sekrit" not in stored                                                   # the key is never stored

    def test_missing_key_waits(self, curated, owner_client, monkeypatch):
        monkeypatch.setenv("E13_AGENT_URL", "http://127.0.0.1:9/v1")
        monkeypatch.setenv("E13_AGENT_KEY_ENV", "NOT_SET_ANYWHERE")
        owner_client.post(url(ITEM, "utterances/text"), params={"batch": "cur"}, json={"text": "13 is supported"})
        r = run_jobs()[0]
        assert r["status"] == "waiting" and "NOT_SET_ANYWHERE" in r["error"]

    def test_new_utterance_supersedes_a_queued_agent_turn(self, curated, owner_client):
        from e13_labeler.db import get_db

        for t in ("first", "second"):
            owner_client.post(url(ITEM, "utterances/text"), params={"batch": "cur"}, json={"text": t})
        with get_db() as conn:
            st = [r[0] for r in conn.execute("SELECT status FROM jobs ORDER BY id")]
        assert st == ["superseded", "queued"]

    def test_bad_agent_output_is_kept_with_problems(self, curated, owner_client, monkeypatch):
        bad = edits(add_clauses=[{"span": {"nodes": [2], "phrase": False}, "stance": "supported", "evidence": [],
                                  "omission": False}], change_clauses=[{"clause": "c9", "stance": "supported",
                                                                        "omission": None, "span": None,
                                                                        "add_evidence": [], "remove_evidence": []}])
        agent = Fake([chat_reply(bad)])
        monkeypatch.setenv("E13_AGENT_URL", agent.url + "/v1")
        owner_client.post(url(ITEM, "utterances/text"), params={"batch": "cur"}, json={"text": "2 is supported"})
        try:
            run_jobs()
        finally:
            agent.close()
        p = ws(owner_client)["proposal"]
        assert any("premise, not the hypothesis" in x for x in p["problems"])
        assert any("no clause 'c9'" in x for x in p["problems"])
        assert owner_client.post(f"/api/annotator/proposals/{p['id']}/reject").json()["status"] == "rejected"

    def test_edited_accept_and_typed_notes(self, curated, owner_client, monkeypatch):
        agent = Fake([chat_reply(AGENT_ANSWER)])
        monkeypatch.setenv("E13_AGENT_URL", agent.url + "/v1")
        owner_client.post(url(ITEM, "utterances/text"), params={"batch": "cur"}, json={"text": "…"})
        try:
            run_jobs()
        finally:
            agent.close()
        p = ws(owner_client)["proposal"]
        only = {"start": HYP.index("man"), "end": HYP.index("man") + 3, "text": "man", "stance": "supported",
                "evidence": [{"start": PREMISE.index("man"), "end": PREMISE.index("man") + 3, "text": "man"}]}
        r = owner_client.post(f"/api/annotator/proposals/{p['id']}/accept",
                              json={"edits": {"item_id": ITEM, "clauses": [only]}})
        assert r.status_code == 200
        assert ws(owner_client)["annotation"]["label"] == "entailment"
        r = owner_client.post(url(ITEM, "notes"), params={"batch": "cur"},
                              json={"category": "grammar", "text": "13 is the subject", "nodes": [13]})
        nid = r.json()["note_id"]
        assert owner_client.post(f"/api/annotator/notes/{nid}/retract").json()["status"] == "retracted"
        assert all(n["id"] != nid for n in ws(owner_client)["notes"])

    def test_history_export(self, curated, owner_client, monkeypatch, tmp_path):
        from e13_labeler.db import get_db
        from e13_labeler.exports import write_export
        from e13_labeler.records import Filters

        flip = edits(remove_clauses=["c2", "c3"], questions=["Is 'a room' its own clause?"])
        agent = Fake([chat_reply(AGENT_ANSWER), chat_reply(flip)])
        monkeypatch.setenv("E13_AGENT_URL", agent.url + "/v1")
        try:
            owner_client.post(url(ITEM, "utterances/text"), params={"batch": "cur"},
                              json={"text": "15 contradicts 4, I think"})
            run_jobs()
            owner_client.post(f"/api/annotator/proposals/{ws(owner_client)['proposal']['id']}/accept", json={})
            owner_client.post(url(ITEM, "utterances/text"), params={"batch": "cur"},
                              json={"text": "actually maybe only 13 matters"})
            run_jobs()
            owner_client.post(f"/api/annotator/proposals/{ws(owner_client)['proposal']['id']}/accept", json={})
        finally:
            agent.close()
        with get_db() as conn:
            m = write_export(conn, filters=Filters(), out_root=tmp_path / "ex", n_boot=10)
        assert "history.jsonl" in {f["name"] for f in m["files"]}
        out = tmp_path / "ex" / m["directory"].split("/")[-1]
        (row,) = [json.loads(line) for line in (out / "history.jsonl").read_text().splitlines()]
        s = row["signals"]
        assert [v["label"] for v in row["versions"]] == ["contradiction", "entailment"]
        assert s["n_versions"] == 2 and s["label_flips"] == 1 and s["utterances"] == 2
        assert s["hedged_notes"] == 1 and s["hedge_words_spoken"] >= 2 and s["agent_questions"] == 1
        assert all(v["from_proposal"] for v in row["versions"])

    def test_reference_opens_after_my_answer_and_marks_later_versions(self, curated, owner_client, tmp_path):
        from e13_labeler.db import get_db
        from e13_labeler.exports import history_rows
        from e13_labeler.importer import import_file
        from e13_labeler.records import Filters

        ref = {"dataset": "esnli", "family": "nli_evidence", "gold": {"label": "contradiction"},
               "evidence": [{"side": "premise", "start": 9, "end": 17, "text": "standing"}], "clauses": [],
               "relations": [], "notes": None, "blank": []}
        row = {**ROWS[0], "id": "r9", "e13": {"reference": ref}}
        path = tmp_path / "ref.jsonl"
        path.write_text(json.dumps(row) + "\n")
        with get_db() as conn:
            import_file(conn, path, batch="cur", task_type="clauses")
        item = "r9#clauses"
        assert ws(owner_client, item)["has_reference"] is True
        assert "reference" not in json.dumps(ws(owner_client, item)["question"])   # never in the payload
        r = owner_client.get(url(item, "reference"), params={"batch": "cur"})
        assert r.status_code == 403
        clause = {"start": HYP.index("walking"), "end": HYP.index("walking") + 7, "text": "walking",
                  "stance": "contradicted", "evidence": [{"start": 9, "end": 17, "text": "standing"}]}
        assert owner_client.post(url(item, "annotations"), params={"batch": "cur"},
                                 json={"item_id": item, "clauses": [clause]}).status_code == 200
        r = owner_client.get(url(item, "reference"), params={"batch": "cur"})
        assert r.status_code == 200 and r.json()["reference"]["gold"]["label"] == "contradiction"
        assert ws(owner_client, item)["reference_seen_at"]
        import time
        time.sleep(1.1)   # timestamps are per second
        owner_client.post(url(item, "annotations"), params={"batch": "cur"}, json={"item_id": item, "clauses": [clause]})
        with get_db() as conn:
            (h,) = [x for x in history_rows(conn, Filters()) if x["item_id"] == item]
        assert [v["after_reference"] for v in h["versions"]] == [False, True] and h["reference_seen_at"]

    def test_short_utterances_stack_and_nothing_unmentioned_is_dropped(self, curated, owner_client, monkeypatch):
        """Three short comments -> three pending proposals, each seeing the ones before; accept applies them all."""
        second = edits(add_clauses=[CLAUSE_WALKING], add_relations=[
            {"from": {"nodes": [4], "phrase": False}, "to": {"nodes": [15], "phrase": False}, "type": "contradicts",
             "note": "standing vs walking"}])
        third = edits(change_clauses=[{"clause": "c2", "stance": "undetermined", "omission": None, "span": None,
                                       "add_evidence": [], "remove_evidence": []}],
                      add_relations=[REFERENT])
        agent = Fake([chat_reply(edits(add_clauses=[CLAUSE_MAN])), chat_reply(second), chat_reply(third)])
        monkeypatch.setenv("E13_AGENT_URL", agent.url + "/v1")
        try:
            for text in ("13 is supported by 2.", "15 contradicts 4; 4 contradicts 15.", "make 15 undetermined; 2 is 13's referent"):
                owner_client.post(url(ITEM, "utterances/text"), params={"batch": "cur"}, json={"text": text})
                run_jobs()
        finally:
            agent.close()
        w = ws(owner_client)
        assert [p["id"] for p in w["proposals"]] == [1, 2, 3]                      # nothing superseded
        prompts = [json.loads(r["body"])["messages"][1]["content"] for r in agent.requests]
        assert '"id": "c1"' in prompts[1] and '"text": "man"' in prompts[1]          # turn 2 sees turn 1's clause
        assert prompts[2].count("(NEW, #") == 1 and prompts[2].count("(earlier, #") == 2
        v = w["pending_view"]
        assert [(c["text"], c["stance"]) for c in v["clauses"]] == [("man", "supported"), ("walking", "undetermined")]
        assert sorted(r["type"] for r in v["relations"]) == ["contradicts", "referent"]
        # A manual save in between is kept: accept applies the edits to the CURRENT version
        manual = {"start": HYP.index("room"), "end": HYP.index("room") + 4, "text": "room", "stance": "unaddressed",
                  "evidence": []}
        owner_client.post(url(ITEM, "annotations"), params={"batch": "cur"}, json={"item_id": ITEM, "clauses": [manual]})
        r = owner_client.post(f"/api/annotator/proposals/3/accept", json={})
        assert r.status_code == 200 and r.json()["accepted"] == [1, 2, 3], r.text
        w = ws(owner_client)
        assert [(c["text"], c["stance"]) for c in w["annotation"]["clauses"]] == [
            ("man", "supported"), ("walking", "undetermined"), ("room", "unaddressed")]
        assert sorted(r["type"] for r in w["annotation"]["relations"]) == ["contradicts", "referent"]
        assert w["proposals"] == [] and w["annotation"]["version"] == 2

    def test_manual_save_can_remove_relations(self, curated, owner_client, monkeypatch):
        agent = Fake([chat_reply(edits(add_clauses=[CLAUSE_MAN], add_relations=[REFERENT]))])
        monkeypatch.setenv("E13_AGENT_URL", agent.url + "/v1")
        owner_client.post(url(ITEM, "utterances/text"), params={"batch": "cur"}, json={"text": "2 is 13's referent"})
        try:
            run_jobs()
        finally:
            agent.close()
        owner_client.post(f"/api/annotator/proposals/{ws(owner_client)['proposal']['id']}/accept", json={})
        a = ws(owner_client)["annotation"]
        body = {"item_id": ITEM, "clauses": [{k: c[k] for k in ("start", "end", "text", "stance", "evidence")}
                                             for c in a["clauses"]]}
        owner_client.post(url(ITEM, "annotations"), params={"batch": "cur"}, json=body)       # null: relations kept
        assert len(ws(owner_client)["annotation"]["relations"]) == 1
        owner_client.post(url(ITEM, "annotations"), params={"batch": "cur"}, json={**body, "relations": []})
        assert ws(owner_client)["annotation"]["relations"] == []

    def test_other_labelers_cannot_touch_my_proposals(self, curated, owner_client, fresh_client, monkeypatch):
        from tests.conftest import login, make_labeler

        agent = Fake([chat_reply(AGENT_ANSWER)])
        monkeypatch.setenv("E13_AGENT_URL", agent.url + "/v1")
        owner_client.post(url(ITEM, "utterances/text"), params={"batch": "cur"}, json={"text": "…"})
        try:
            run_jobs()
        finally:
            agent.close()
        pid = ws(owner_client)["proposal"]["id"]
        owner_client.post("/api/auth/logout")
        make_labeler("other", clearance="internal")
        login(fresh_client, "other")
        assert fresh_client.post(f"/api/annotator/proposals/{pid}/accept", json={}).status_code == 404
        assert ws(fresh_client)["proposal"] is None
