# Annotator mode: a curated dataset annotated by speaking, with an offline agent scribe

**Status:** spec and phase 1 build on branch `e13/annotator` (2026-10-08). The owner set the direction in conversation
on 2026-10-08. It builds on the clauses task (`CLAUSE_TASK.md`) and keeps it as the label core.

## 1. Why

On `e17c-esnli-50`, the owner's clause labels scored 0.86 against the SNLI majority gold, the best of any method E17
scored. The most valuable material, though, was the discussion around the labels:
- "holding a chicken *suggests* outside, it doesn't entail it";
- "gentleman" read as a loose synonym of "man", or as a claim about manners;
- "window shopping" turning into a contradiction if the premise said he was buying.

This material is spoken, it is about specific words and phrases, and it changes as the owner thinks. Annotator mode
captures it as data.

It also matters beyond SNLI. The same tool applies when the premise is an event-log entry and the hypothesis is an
escalation procedure. E16 measured question-side adapters at k = 32–800 examples: loraq scored 0.786 at 500 and 0.819
at 800. A small number of deeply annotated items, multiplied by their variations, might reach the high hundreds an
adapter needs for one end-user question. That is the experiment this tool makes possible (§9).

## 2. Principles

1. **The human judges; the agent is a scribe.** The agent turns what the owner said into structure and proposes it;
   the owner accepts or corrects. The agent never sees gold, model answers or probe output. (E17: agents holding a
   probe copied it 97–99% of the time.)
2. **Recording is instant; processing is offline.** The front end only records. Transcription and agent turns are jobs
   in a queue that run when an engine is available. The owner can leave a comment on each of several items and come
   back after the queue has caught up. When engines are up, the same queue gives a live view.
3. **Nothing is overwritten.** Every annotation version, utterance, transcript, proposal and note is kept. The history
   is a dataset in its own right: hedging notes and back-and-forth relabelling mark hard items, which are the targets
   for authoring variations.
4. **The label core stays the clauses task.** Clauses, stances, evidence and the derived label work exactly as in
   `CLAUSE_TASK.md`. Annotator mode adds numbered nodes, relations, notes, utterances and proposals around that core.

## 3. Curated dataset instead of a queue

A batch can be `curated` instead of `queue`:

| | `queue` (today) | `curated` |
|---|---|---|
| Order | the server serves the next item | the labeller browses a list and opens any item |
| Revisiting | edit only the last 20 submissions | open any own annotation at any time; saving makes a new version |
| Status shown | none | per item: unlabelled / labelled (label) / utterances waiting / jobs running / proposal to review / versions |
| Locks, overlap targets, hidden gold | yes | no (curation is not blind measurement; reliability comes from separate queue batches) |

The quiz and onboarding stay in place for queue batches and new labellers. Curated batches are for the owner and
trusted annotators.

## 4. The numbered view

Both texts are shown as numbered trees, with **one numbering across both texts**: premise nodes first, then hypothesis
nodes. Numbers are stable for a given parse; the parse is stored with its parser version, and every utterance records
the parse it was made against.

| Input | Tree | A node is |
|---|---|---|
| Text | spaCy dependency parse (`en_core_web_sm`) | one word, carrying its POS tag, dependency label and head |
| JSON | the r1 rendering (rule 7) | one field (`key: value`, pointer); the words of long string values are parsed as text below the field (phase 2) |
| Code | tree-sitter | later |

**What a spoken number means.** "9" means node 9's **phrase**: its subtree for text, or its field for JSON. "Word 9"
means the single word. On `e17c-esnli-50`, 136 of the owner's 139 clauses were a single-head fragment of the parse, so
naming the head names the clause.

The view shows each sentence inline with small node numbers, and an outline tree under it listing
`number word POS dep → head`.

## 5. The pipeline

```
record (or type) ──► utterance ──► transcribe job ──► agent job ──► proposal ──► owner review ──► annotation version
                         ▲                                                     │
                         └──────────── follow-up recording ◄───────────────────┘
```

| Stage | Engine | When unavailable |
|---|---|---|
| Record | browser `MediaRecorder` (webm/opus); works over the SSH tunnel because `http://localhost` is a secure context | — |
| Typed input | the same utterance, with the text given directly | — |
| Transcribe | any OpenAI-compatible `/v1/audio/transcriptions` endpoint (`E13_STT_URL`), e.g. the owner's `~/audio_server/localoai_transcription.py` (whisper-turbo) | the job waits (`waiting`) and is retried when the engine answers |
| Agent | any OpenAI-compatible `/v1/chat/completions` endpoint (`E13_AGENT_URL`, `E13_AGENT_MODEL`), e.g. `llama-server` with Qwen3.6-27B; JSON-schema-constrained output | the job waits |
| Review | the owner, in the item view | proposals stay `pending` |

- **The worker** is a separate process (`python -m e13_labeler worker`). It polls the jobs table, so the web server
  never blocks on a model, and it can run on another machine against the same endpoints.
- **Order:** one item's jobs run in order. A new utterance on an item supersedes that item's queued agent job, so the
  agent always sees every utterance so far.

## 6. What the agent receives and returns

**Input:**
- the numbered nodes of both texts (word, POS, dep, head, phrase text);
- the current annotation (clauses, evidence, label) expressed in node numbers;
- the existing notes;
- every transcript for the item, with the new ones marked.

It never receives gold, model answers or source names.

**Output** (JSON-schema-constrained):

```json
{
  "clauses": [{"nodes": [9], "phrase": true, "stance": "undetermined", "evidence": [{"nodes": [3], "phrase": true}],
               "omission": false}],
  "relations": [{"from": [11], "to": [4], "type": "suggests", "note": "looking is related to window shopping, which is more specific"}],
  "notes": [{"target": {"nodes": [11, 4]}, "category": "lexical", "text": "window shopping = looking without intent to buy",
             "hedge": false}],
  "label_override": null,
  "completion": null,
  "questions": ["Did you mean 'at a man' as a separate clause from 'looking up'?"]
}
```

- **Relation types:** `referent`, `supports`, `suggests`, `contradicts`, `more_specific`, `less_specific`, `same_as`,
  `other`.
- **Note categories:** `relation`, `grammar`, `pos`, `lexical`, `label`, `procedure`, `other`.
- `hedge` marks uncertainty in the owner's own words ("I think", "probably", "could be read as").
- **Conversion:** the server turns node references into character offsets: a phrase is the subtree's span, a word is
  the token's span. Clauses and evidence are then validated exactly as in the clauses task.
- **Bad output:** a proposal that fails validation is kept with its problems listed, and is shown to the owner.

## 7. Review

The item view shows the pending proposal as a diff against the current annotation: clauses added or changed,
evidence, relations, notes and the agent's questions.

| Action | Result |
|---|---|
| Accept | becomes the next annotation version; notes and relations attach to it |
| Edit, then accept | the same, after manual edits |
| Reject | kept, marked rejected |
| Reply | record or type a follow-up; this queues another agent turn with all utterances so far |

## 8. Variations (phase 3)

A variation is a counterfactual edit the owner states: "if 5 said 'riding', it'd be neutral, because he could be
riding something other than a motorcycle".
- **Process:** the agent writes the edited pair and the owner confirms the text and the label. The variation becomes a
  new item linked to its parent: the parent id, the edit (node, from, to) and the reason.
- **Why:** these minimal pairs are contrast sets (Gardner et al. 2020) and counterfactually augmented data (Kaushik et
  al. 2020).
- **Caution:** treat variations as single-author until someone relabels a sample blind. On `e17c-esnli-50`, the
  caption writers' own labels matched the SNLI majority on only 45 of 50 items.

## 9. Post-processing (phase 4), and the experiment this enables

- **History signals:**
  - the number of versions;
  - label flips between versions;
  - hedged notes;
  - the time spent;
  - agent questions the owner had to answer.

  Together these mark hard items, the targets for variations.
- **Note mining:** cluster the notes by category and text. Extract specificity pairs and lexical relations.
- **Template generation:** hypernym and specificity pairs from the notes, ConceptNet (local build at
  `/storage/conceptnet5_data`) and WordNet fill templates.
  - `IsA`, `Synonym`, `FormOf`, `MannerOf` → entailment-grade;
  - `RelatedTo`, `AtLocation` → suggestion-grade;
  - `Antonym`, `DistinctFrom` → contradiction-grade.

  Keep the template share small and evaluate on natural held-out items (the HANS lesson). ConceptNet's `RelatedTo`
  drops polarity (`window_shop RelatedTo buying` comes from "without intention of buying"), so read glosses before
  trusting an edge.
- **Experiment:** an E16-style curve comparing plain labels at k with deep annotations at k/5 (clauses, notes and about
  5 variations per item), first on NLI and then on an event-log escalation task. The result is how many hours of
  discussion an end-user question costs.

## 10. Data model (schema V10)

| Table | Holds |
|---|---|
| `batches.mode` | `queue` (default) or `curated` |
| `item_parses` | `item_id`, `side` (`premise` / `hypothesis`), `parser`, `nodes_json` (number, text, offsets, POS, tag, dep, head, phrase offsets) |
| `utterances` | `item_id`, `labeler_id`, `batch_id`, `kind` (`label` / `followup` / `variation`), `source` (`audio` / `typed`), audio path / MIME type / duration, `text`, `segments_json`, `stt_engine`, `parse_ids`, `annotation_id` current at the time, created and transcribed times |
| `jobs` | `kind` (`transcribe` / `agent`), `item_id`, `labeler_id`, `utterance_id`, `status` (`queued` / `running` / `waiting` / `done` / `failed` / `superseded`), attempts, error, `result_json`, timestamps |
| `proposals` | `item_id`, `labeler_id`, `job_id`, utterance ids, base annotation, `payload_json`, `problems_json`, agent engine and model, `status` (`pending` / `accepted` / `rejected` / `superseded`), the accepted annotation id, timestamps |
| `notes` | `item_id`, `labeler_id`, `annotation_id`, `target_json` (nodes, clause, relation), `category`, `text`, `hedge`, `source` (`agent` / `typed`), `utterance_id`, `proposal_id`, `retracted_at` |
| `relations` | `annotation_id`, `from_json` / `to_json` (node ranges and offsets), `type`, `note` |

Audio files live under `E13_OUTPUTS/audio/<item>/<utterance>.webm`. The no-delete triggers cover all of the new
tables.

## 11. Phases

| Phase | Scope | Needs a GPU |
|---|---|---|
| 1 | curated batches and the dataset list; numbered trees; utterances (audio and typed) and the job queue; the worker with HTTP STT and agent stages; proposals and review; notes and relations; the history export | no (the engines are external) |
| 2 | JSON and code trees; live job view polish; agent prompt tuning on real recordings | runs engines |
| 3 | variations authoring | runs engines |
| 4 | history and note mining, template generation, the k vs deep-k experiment | yes |
