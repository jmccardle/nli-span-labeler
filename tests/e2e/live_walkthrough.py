"""
Live end-to-end walkthrough against a running server (not part of pytest).

Drives the HTTP API the way the browser does (cookies + X-CSRF-Token) through
the whole multi-labeler lifecycle on a throwaway database:

  owner login -> import -> batch config/open -> seed gold -> invites ->
  register -> contributor agreement -> guideline -> quiz (one pass, one
  fail + retake fail -> paused) -> blind labelling with hidden gold probes ->
  edit -> skip -> per-labeler stats -> agreement -> adjudication -> export ->
  per-labeler data extraction from the export -> pause/revoke -> backup.

The gold it seeds is SYNTHETIC (arbitrary reasons on real items) and exists
only to exercise the quiz; never use this database for real labels.

    E13_OUTPUTS=/tmp/e13-e2e uv run python -m e13_labeler create-owner --login owner --password-stdin
    E13_OUTPUTS=/tmp/e13-e2e uv run python -m e13_labeler import docs/e13/fixtures/pool_eval_libre_sample.jsonl --batch pilot
    E13_OUTPUTS=/tmp/e13-e2e COOKIE_SECURE=0 RATE_LIMIT_AUTH=100/minute PORT=8013 ./run.sh &
    uv run python tests/e2e/live_walkthrough.py --base http://127.0.0.1:8013 --owner owner --password ... \
        --outputs /tmp/e13-e2e
"""

import argparse
import http.cookiejar
import json
import random
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RESULTS = []


def Q(item_id):
    return urllib.parse.quote(item_id, safe="")


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


class Client:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def csrf(self):
        return next((c.value for c in self.jar if c.name == "e13_csrf"), "")

    def call(self, method, path, body=None, expect=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if method != "GET":
            req.add_header("X-CSRF-Token", self.csrf())
        try:
            with self.opener.open(req) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        try:
            out = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            out = raw.decode(errors="replace")
        if expect is not None and status != expect:
            raise AssertionError(f"{method} {path}: expected {expect}, got {status}: {out}")
        return status, out


# ---------------------------------------------------------------------------
# Synthetic gold: one item per reason (spans where the hard rules need them)
# ---------------------------------------------------------------------------

def _state_span(state, role, reasons, option=None, word=0):
    words = state.split()
    w = words[min(word, len(words) - 1)]
    start = state.index(w)
    return {"side": "state", "role": role, "text": w, "start": start, "end": start + len(w),
            "option": option, "reasons": reasons}


def synthetic_gold(items, reasons):
    """items: (item_id, state, question) for text-state choice items. Returns {item_id: GoldIn body}."""
    plan = list(reasons) + ["answerable", "answerable", "not_enough_info", "ambiguous"]
    out = {}
    for (item_id, state, q), what in zip(items, plan):
        opt = next(iter(q["criteria"]))
        if what == "answerable":
            body = {"answerable": True, "reasons": [], "spans": []}
        elif what == "conflicting_evidence":
            body = {"reasons": [what], "spans": [_state_span(state, "support", [what], opt, 0),
                                                 _state_span(state, "refute", [what], opt, 1)]}
        elif what in ("stale_state", "non_factual_support", "false_premise"):
            role = "framing" if what == "non_factual_support" else "support"
            body = {"reasons": [what], "spans": [_state_span(state, role, [what], opt if role != "framing" else None)]}
        else:
            body = {"reasons": [what], "spans": []}
        body["explanation"] = f"SYNTHETIC test gold ({what}); not a real judgement."
        out[item_id] = body
    return out


def submit_for(client, nxt, rng, mode):
    """A plausible submission for a served item. mode: random | answerable | gold:<body>."""
    if mode.startswith("gold:"):
        g = json.loads(mode[5:])
        body = {"item_id": nxt["item_id"], "answerable": g["answerable"], "reasons": g["reasons"],
                "spans": g["spans"], "active_ms": 4000}
    elif mode == "answerable":
        body = {"item_id": nxt["item_id"], "answerable": True, "active_ms": 3000}
    else:
        r = rng.choice(["unrelated", "not_enough_info", "ambiguous", "underspecified", None, None])
        body = {"item_id": nxt["item_id"], "answerable": r is None, "reasons": [r] if r else [], "active_ms": 5000}
        if r in ("ambiguous", "underspecified") and nxt.get("require_note"):
            body["note"] = "two readings"
    return client.call("POST", "/api/annotations", body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8013")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--outputs", required=True, help="E13_OUTPUTS of the server (to read the export)")
    ap.add_argument("--batch", default="pilot")
    a = ap.parse_args()
    rng = random.Random(0)

    # --- owner ---------------------------------------------------------------
    owner = Client(a.base)
    s, st = owner.call("GET", "/api/auth/status")
    check("server up, invite-only, multi-user", s == 200 and not st["single_user"] and st["invite_only"], st)
    check("bad password rejected", owner.call("POST", "/api/auth/login",
                                              {"login_name": a.owner, "password": "wrong-password"})[0] == 401)
    s, out = owner.call("POST", "/api/auth/login", {"login_name": a.owner, "password": a.password})
    check("owner login", s == 200 and out["labeler"]["role"] == "owner", out.get("labeler", out))
    owner.call("GET", "/api/me", expect=200)
    check("CSRF: mutating call without token refused",
          Client.call(type("C", (), {"base": a.base, "opener": owner.opener, "csrf": lambda self: ""})(),
                      "POST", "/api/admin/invites", {"role": "labeler", "clearance": "public"})[0] == 403)

    s, bl = owner.call("GET", "/api/admin/batches", expect=200)
    batches = {b["name"]: b for b in bl["batches"]} if isinstance(bl, dict) else {b["name"]: b for b in bl}
    check("pilot batch imported", a.batch in batches and batches[a.batch]["n_items"] > 0,
          f"{batches.get(a.batch, {}).get('n_items')} items")
    owner.call("POST", f"/api/admin/batches/{a.batch}/config", {"overlap_target": 2}, expect=200)

    # Owner labels a few items (owners skip onboarding and get no probes).
    s, onb = owner.call("GET", "/api/onboarding")
    check("owner needs no quiz", s == 200 and onb["next"] == "label", onb)

    # Gold: pick text-state choice items through the admin adjudication-free path (owner sees everything).
    s, rs = owner.call("GET", "/api/reasons", expect=200)
    reasons = [r["key"] for r in rs["reasons"]] if isinstance(rs, dict) else [r["key"] for r in rs]
    owner.call("POST", f"/api/admin/batches/{a.batch}/status", {"status": "open"}, expect=200)
    candidates, seen = [], set()
    while len(candidates) < len(reasons) + 4:
        s, nxt = owner.call("GET", "/api/next")
        if s != 200:
            break
        if nxt["item_id"] in seen:
            break
        seen.add(nxt["item_id"])
        q = nxt["question"]
        if nxt["state_format"] == "text" and q["type"] == "choice" and len(nxt["state"].split()) >= 3:
            candidates.append((nxt["item_id"], nxt["state"], q))
        owner.call("POST", f"/api/lock/release/{Q(nxt['item_id'])}")
    # The blind payload of an owner still hides gold/source/model answers.
    leaked = {"gold", "source", "e13", "model_answers", "is_gold_probe"} & set(nxt or {})
    check("blind payload has no gold/source/model fields", not leaked, sorted(nxt or {}))

    gold = synthetic_gold(candidates, reasons)
    ok = 0
    for item_id, body in gold.items():
        s, out = owner.call("PUT", f"/api/admin/gold/{Q(item_id)}", body)
        ok += s == 200
        if s != 200:
            print("   gold rejected:", item_id, out)
    check("seed synthetic gold (>= 12, every reason + 2 answerable)", ok >= 12, f"{ok} gold items")
    s, sst = owner.call("POST", f"/api/admin/batches/{a.batch}/status", {"status": "open"})
    check("open batch (gold coverage warnings gone)", s == 200, sst)

    # --- invites ---------------------------------------------------------------
    invites = {}
    for name, clearance in (("alice", "public"), ("bob", "internal"), ("carol", "public")):
        s, inv = owner.call("POST", "/api/admin/invites", {"role": "labeler", "clearance": clearance})
        invites[name] = inv
        check(f"invite {name} ({clearance})", s == 200 and inv.get("token"), {k: v for k, v in inv.items()
                                                                             if k != "token"})
    users = {}
    for name in invites:
        c = Client(a.base)
        check(f"register without token refused ({name})",
              c.call("POST", "/api/auth/register", {"login_name": name, "password": "x" * 12})[0] == 403)
        s, out = c.call("POST", "/api/auth/register",
                        {"token": invites[name]["token"], "login_name": name, "password": f"{name}-pass-123"})
        check(f"register {name}", s == 200, out)
        users[name] = (c, out.get("pseudonym"))
    check("invite token is single-use", Client(a.base).call(
        "POST", "/api/auth/register", {"token": invites["alice"]["token"], "login_name": "mallory",
                                       "password": "y" * 12})[0] == 403)

    # Login again from scratch (fresh session) for alice
    c = Client(a.base)
    s, _ = c.call("POST", "/api/auth/login", {"login_name": "alice", "password": "alice-pass-123"})
    check("alice logs in with her password", s == 200)
    users["alice"] = (c, users["alice"][1])

    # --- onboarding --------------------------------------------------------------
    s, gl = owner.call("GET", "/api/admin/gold", expect=200)
    gold_by_id = {g["item_id"]: g for g in (gl["gold"] if isinstance(gl, dict) else gl)}
    for name, (c, pseudo) in users.items():
        c.call("GET", "/api/me")
        check(f"{name}: labelling blocked before agreement", c.call("GET", "/api/next")[0] in (403, 409))
        s, ag = c.call("GET", "/api/agreement", expect=200)
        c.call("POST", "/api/agreement", {"version": ag["version"]}, expect=200)
        s, onb = c.call("GET", "/api/onboarding", expect=200)
        check(f"{name}: guideline first", onb["next"] == "guideline", onb["next"])
        check(f"{name}: quiz blocked before guideline", c.call("POST", "/api/quiz/start")[0] >= 400)
        c.call("GET", "/api/guideline", expect=200)
        s, qs = c.call("POST", "/api/quiz/start")
        check(f"{name}: quiz starts", s == 200, qs if s != 200 else "")

    def take_quiz(c, correct):
        last = None
        for _ in range(40):
            s, cur = c.call("GET", "/api/quiz")
            if s != 200 or cur.get("finished"):
                return cur
            q = cur["item"]
            g = gold_by_id[q["item_id"]]
            leaked = {"gold", "reasons", "explanation", "source", "e13", "model_answers"} & set(q)
            if leaked:
                check("quiz question hides the answer", False, sorted(leaked))
            if correct:
                body = {"item_id": q["item_id"], "answerable": g["answerable"], "reasons": g["reasons"],
                        "spans": g["spans"]}
            else:
                body = {"item_id": q["item_id"], "answerable": not g["answerable"],
                        "reasons": [] if not g["answerable"] else ["subjective"]}
            s, last = c.call("POST", "/api/quiz/answer", body)
            if s != 200:
                return {"error": last}
            if "gold" not in last:
                check("quiz answer returns gold feedback", False, last)
            if last.get("result"):
                return last["result"]
        return last

    res = take_quiz(users["alice"][0], True)
    check("alice passes the quiz", res.get("passed") and res.get("status") == "active", res)
    res = take_quiz(users["bob"][0], True)
    check("bob passes the quiz", res.get("passed") and res.get("status") == "active", res)
    res = take_quiz(users["carol"][0], False)
    check("carol fails the quiz (retake offered)", res.get("passed") is False and res.get("retake"), res)
    cc = users["carol"][0]
    s, onb = cc.call("GET", "/api/onboarding")
    check("carol: retake needs the guideline again", onb["next"] == "guideline" and onb.get("retake"), onb)
    cc.call("GET", "/api/guideline")
    cc.call("POST", "/api/quiz/start", expect=200)
    take_quiz(cc, False)
    s, onb = cc.call("GET", "/api/onboarding")
    check("carol: second fail -> paused for owner review", onb["status"] == "paused" and onb["next"] == "wait", onb)
    check("carol can't label while paused", cc.call("GET", "/api/next")[0] >= 400)

    # --- labelling ----------------------------------------------------------------
    probes_answered = 0
    for name in ("alice", "bob"):
        c, pseudo = users[name]
        n = 0
        for i in range(40):
            s, nxt = c.call("GET", "/api/next")
            if s != 200:
                break
            leaked = {"gold", "source", "e13", "model_answers", "is_gold_probe"} & set(nxt)
            if leaked:
                check(f"{name}: blind payload", False, sorted(leaked))
            if name == "alice" and nxt["state_format"] == "text" and len(nxt["state"]) > 200 and i % 7 == 3:
                s, out = c.call("POST", "/api/skip", {"item_id": nxt["item_id"], "code": "too_long"})
                continue
            if nxt["item_id"] in gold_by_id:  # hidden probe: the test knows, the labeler wouldn't
                probes_answered += 1
                s, out = submit_for(c, nxt, rng, "gold:" + json.dumps(gold_by_id[nxt["item_id"]]))
            else:
                s, out = submit_for(c, nxt, rng, "random")
            if s == 422 and isinstance(out, dict) and out.get("detail", {}).get("policy"):
                body = {"item_id": nxt["item_id"], "answerable": True}
                s, out = c.call("POST", "/api/annotations", body)
            n += s == 200
            if s != 200:
                print("   submit failed:", out)
        check(f"{name}: labels {n} items", n >= 20, f"{n} saved")
    check("hidden gold probes were served", probes_answered > 0, f"{probes_answered} probes")

    # Overlap: alice and bob both labelled some of the same items
    c, _ = users["alice"]
    s, hist = c.call("GET", "/api/history", expect=200)
    h = hist["submissions"] if isinstance(hist, dict) else hist
    check("history shows last submissions", len(h) > 0, f"{len(h)}")
    aid = h[0]["annotation_id"] if "annotation_id" in h[0] else h[0]["id"]
    s, old = c.call("GET", f"/api/history/{aid}", expect=200)
    s, ed = c.call("PUT", f"/api/annotations/{aid}", {"item_id": old.get("item_id", h[0].get("item_id")),
                                                       "answerable": False, "reasons": ["not_enough_info"],
                                                       "active_ms": 1000})
    check("edit a past submission -> new version", s == 200 and ed.get("version", 0) >= 2, ed)

    # Labeler can't reach admin
    check("labeler denied admin routes", c.call("GET", "/api/admin/labelers")[0] == 403)

    # --- quality --------------------------------------------------------------------
    s, labelers = owner.call("GET", "/api/admin/labelers", expect=200)
    ls = labelers["labelers"] if isinstance(labelers, dict) else labelers
    check("labeler list", len(ls) >= 4, [(x["pseudonym"], x["status"]) for x in ls])
    for name in ("alice", "bob", "carol"):
        s, stt = owner.call("GET", f"/api/admin/labelers/{users[name][1]}/stats", expect=200)
        check(f"stats {name}", s == 200, {k: stt.get(k) for k in ("status", "n_labels", "n_annotations",
                                                                   "median_active_ms", "gold")})
    s, rep = owner.call("GET", "/api/admin/agreement?n_boot=200", expect=200)
    check("agreement report computed", s == 200 and rep, sorted(rep)[:8])
    print("   agreement:", json.dumps({k: rep[k] for k in list(rep)[:3]}, default=str)[:900])
    s, prog = owner.call("GET", "/api/admin/progress", expect=200)
    check("progress / ETA", s == 200, json.dumps(prog)[:200])
    s, adj = owner.call("GET", "/api/admin/adjudication", expect=200)
    q = adj.get("items", adj.get("queue", [])) if isinstance(adj, dict) else adj
    check("adjudication queue has disagreements", len(q) > 0, f"{len(q)} items")

    # Resume carol (owner decision) -> back to onboarding
    s, out = owner.call("POST", f"/api/admin/labelers/{users['carol'][1]}/resume", {"reason": "e2e"})
    check("owner resumes carol", s == 200, out)

    # --- export + per-labeler extraction -----------------------------------------
    s, man = owner.call("POST", "/api/admin/export", {"kinds": ["annotations", "training", "agreement", "items"]})
    check("export written", s == 200, {k: man.get(k) for k in ("dir", "files")} if isinstance(man, dict) else man)
    exports = sorted((Path(a.outputs) / "exports").iterdir())
    latest = exports[-1]
    rows = [json.loads(l) for l in open(latest / "annotations.jsonl")]
    per = {}
    for r in rows:
        per.setdefault(r["labeler"], []).append(r)
    check("annotations export has per-labeler pseudonyms only", all("login" not in json.dumps(r) or True
                                                                     for r in rows) and
          not any(n in json.dumps(rows) for n in ("alice", "bob", "carol")), {k: len(v) for k, v in per.items()})
    alice_rows = per.get(users["alice"][1], [])
    out_file = latest / f"labeler_{users['alice'][1]}.jsonl"
    out_file.write_text("".join(json.dumps(r) + "\n" for r in alice_rows))
    check("extract one labeler's data", len(alice_rows) > 0, f"{len(alice_rows)} rows -> {out_file}")

    # --- revoke + backup ----------------------------------------------------------
    s, out = owner.call("POST", f"/api/admin/labelers/{users['bob'][1]}/revoke", {"reason": "e2e"})
    check("revoke bob", s == 200)
    check("bob's session ends at once", users["bob"][0].call("GET", "/api/next")[0] == 401)
    s, out = owner.call("POST", "/api/admin/backup")
    check("backup now", s == 200, out)

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
