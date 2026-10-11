"""
Fake OpenAI-compatible engines for the annotator e2e test (no model runs):
  POST /v1/audio/transcriptions -> a fixed transcript
  POST /v1/chat/completions     -> a fixed annotation in node numbers (for the e2e pair)

    python tests/e2e/fake_engines.py PORT
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Premise "A man is standing in the doorway of a building." -> nodes 1-11;
# hypothesis "The man is walking into a room." -> nodes 12-19
EMPTY = {"add_clauses": [], "change_clauses": [], "remove_clauses": [], "add_relations": [], "remove_relations": [],
         "notes": [], "label_override": None, "completion": None, "questions": []}
ANSWER = {**EMPTY,
    "add_clauses": [{"span": {"nodes": [13], "phrase": False}, "stance": "supported",
                     "evidence": [{"nodes": [2], "phrase": False}], "omission": False},
                    {"span": {"nodes": [15], "phrase": False}, "stance": "contradicted",
                     "evidence": [{"nodes": [4], "phrase": False}], "omission": False},
                    {"span": {"nodes": [16], "phrase": True}, "stance": "undetermined",
                     "evidence": [{"nodes": [5], "phrase": True}], "omission": False}],
    "add_relations": [{"from": {"nodes": [2], "phrase": False}, "to": {"nodes": [13], "phrase": False},
                       "type": "referent", "note": None}],
    "notes": [{"nodes": [4, 15], "category": "lexical", "text": "standing and walking can't both hold", "hedge": False}],
    "questions": ["Is 'into a room' one clause?"]}
# A short follow-up that only adds a relation (stacks on whatever is pending)
SPECIFIC = {**EMPTY, "add_relations": [{"from": {"nodes": [7], "phrase": False}, "to": {"nodes": [18], "phrase": False},
                                        "type": "less_specific", "note": "a doorway is not yet a room"}]}


def answer_for(body: bytes) -> dict:
    """The newest utterance decides: a comment about specificity gets the relation-only edit."""
    try:
        prompt = json.loads(body)["messages"][1]["content"]
    except (ValueError, KeyError, IndexError):
        return ANSWER
    new = [line for line in prompt.splitlines() if line.startswith("(NEW, #")]
    if new and "specific" in new[-1]:
        return SPECIFIC
    if new and "subject" in new[-1]:
        return SAME
    return ANSWER


SAME = {**EMPTY, "add_relations": [{"from": {"nodes": [1, 2], "phrase": False}, "to": {"nodes": [12, 13], "phrase": False},
                                    "type": "same_as", "note": None}]}


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        if self.path.endswith("/audio/transcriptions"):
            out = {"text": "13 is supported by 2. 15 contradicts 4.", "segments": [{"start": 0, "end": 2, "text": "…"}]}
        elif self.path.endswith("/chat/completions"):
            out = {"choices": [{"message": {"content": json.dumps(answer_for(body))}}]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
