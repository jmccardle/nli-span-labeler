# Hypothesis-clause task: overhaul assessment

Status: assessment, not a build order (2026-10-08). Proposed by the MBNLIv3 training/research session from the owner's
feedback while labelling `e17-esnli-50`. Code references are to branch `e13/local-validation` (`bb5f1af`).

## The proposal

For each item, the labeller repeats these steps until every content clause of the hypothesis is covered:

1. select a hypothesis span (one clause or condition);
2. tag it `supported`, `contradicted` or `unaddressed`;
3. for `supported` and `contradicted`, select the premise words it rests on.

The sentence label is derived, not entered:

- any clause contradicted → contradiction;
- else all clauses supported → entailment;
- else neutral.

Neutral items keep the two completion sentences ("E:" and "C:").

## What exists today

- **The hypothesis cannot be selected.** E17 rows put it inside `question.instructions` (`human_items.py`). Instructions
  render as escaped plain text (`label.js:173`). Only the state and the option descriptions are tokenized into
  selectable containers.
- **Spans** (`db.py:145`) have `side ∈ {state, option}`, a role (`support` / `refute` / `unsupported` / `framing`) and a
  link to the reasons they back.
  - Option-side spans with role `unsupported` are the nearest existing primitive: they mark part of an option's text.
  - No span can point at another span. Linking premise words to a hypothesis clause is the genuinely new thing.
- **There is no label column.** `annotations` holds `answerable`, `reasons_json`, `note` and `relation_json`.
  - E17 infers the label from `not_enough_info` and from which option the support spans name.
  - Spans on two options, or none, leave the item unlabelled.
- **M3 / FR-23 (relation) is unbuilt.**
  - Built so far: the `task_type` CHECK on `batches` allows `reasons+relation` and `relation`; there is an
    `annotations.relation_json` column; `records.py` reads that column back.
  - Not built: no validation, UI, export, agreement or gold.
  - FR-23 is sentence-level: one stance (support / refute / undetermined) per option, plus relevance.
- **Agreement** (`spans_agreement.py`) treats every span as word units (`state|i`, `option:<key>|i`). Gold, quiz and
  hidden-gold scoring (`quality.score`) compare the set `answerable` + reasons.

## Recommendation: a new task type `clauses`, not M3

- **It's a different task.** M3 gives a stance per option for the whole sentence. The clause task gives a stance per
  hypothesis span, with linked evidence.
- **It contains M3 for NLI.** One clause covering the whole hypothesis, labelled supported or contradicted, is exactly
  M3's stance on that hypothesis. It isn't true the other way round.
- **Build order:** build `clauses` first. If M3 is ever built, have it read its stance from clauses where they exist.
  Reuse M3's stance vocabulary (`support` / `refute` / `undetermined` ≡ supported / contradicted / unaddressed), so that
  later the model's relation head and the clause head share label names.
- **It's a separate batch type.** A clause batch does not ask `answerable` or reasons; it keeps skip, flag, note,
  history and edit. NLI pairs are not abstain-reason items, and mixing the two in one form would make both worse.

## What changes

| Area | Change | Size |
|---|---|---|
| **Item format** | A labeller-visible string `question.hypothesis` (add to `QUESTION_KEYS`); the question stays `choice` e/n/c, so the API contract's types are untouched. The importer validates it. E17 rows move the hypothesis out of `instructions`. | S |
| **Schema V9** | (1) New table `clauses`: `id`, `annotation_id`, `idx`, `start`, `end`, `text`, `stance` CHECK in the three values, `note`. (2) `spans.clause_id` nullable FK, added with `ALTER TABLE` (no rebuild). (3) `annotations.label` (the derived label) and `annotations.completion_json` (`{"E": …, "C": …}`), so notes are no longer parsed. (4) Allow `clauses` in the `batches.task_type` CHECK. That needs a table rebuild, the same copy-and-rename pattern V5 already shipped. | M |
| **Validation** | Clause offsets index the hypothesis; the existing whitespace-trim rule applies. Clauses may not overlap. At least one clause. Evidence spans are state-side, linked to a clause, and the clause must be supported or contradicted. A supported or contradicted clause with no evidence is a policy violation, overridable with Shift+Enter (world knowledge, e.g. "a dog" ⊢ "an animal"). The server derives the label and rejects a client label that disagrees. | M |
| **Submit / edit API** | Payload gains `clauses: [{start, end, stance, evidence: [{start, end}], note}]`. Clauses are stored as rows; versions and edits work as today (new version, nothing deleted). History and edit payloads include clauses. | S–M |
| **UI** | A third selectable container, the hypothesis line, above the options. A clause list panel. Live derived label in the footer. Two short inputs for E/C completions on neutral. | M–L |
| **Keyboard** | A region for the hypothesis (cursor and shift-extend as for the state). Select hypothesis words, then `s` / `c` / `u` creates a clause and makes it active. Select premise words, then `s` adds evidence to the active clause. `Tab` / `[` `]` cycle clauses. `Delete`, `z` and `Enter` as today. `c` is free in the current map; `e` (history) must stay. | M |
| **Export** | Annotations export: `clauses` + `label` + `completion`. Training export: per item, hypothesis word tags and per-clause premise evidence as character offsets **and** whitespace word indices (`hypothesis.split()` / `premise.split()`), the indexing E17 uses. Indices are derived at export, never stored, as for rule 7. | S–M |
| **Agreement** | Labellers will segment clauses differently, so don't compare clauses. (1) Sentence label: nominal α on the derived label (reuse `alpha_nominal`). (2) Per hypothesis word: nominal α on the stance of the clause covering the word (`none` if uncovered). (3) Evidence: token F1 per hypothesis word, between the premise words linked to the clause each labeller put that word in. This is also exactly the unit the per-hypothesis-token head is trained on. | M |
| **Gold / quiz / hidden gold** | `gold` and `quality.score` only know reasons. **Real risk:** `_probe_candidate` (`app.py:684`) picks any gold item that the batch's tier filter allows, not one of the batch's own task type. Before any non-owner labels a clause batch, either: (a) restrict probes to the batch's task type; or (b) build clause gold scored on the derived label first, clause stances second. Owners get no probes, so an owner-only first version can defer clause gold, the quiz and adjudication. | S (a) / L (b) |
| **Adjudication, model labels** | Reasons-only today. Defer both. Importing E17 agent certificates as clause pseudo-labels (`model_labels.py`) is a natural later step. | defer |
| **Tests** | Unit tests for validation and the derivation rule, migration, export, agreement, and a browser walkthrough (select a clause → tag → evidence → submit). | M |

**Rough size:**
- **Owner-only first version** (schema, validation, API, UI and keys, export, label and word-level agreement, probes
  restricted by task type): about 1,000–1,300 lines including tests, roughly 1–2 days of agent work.
- **Clause gold, quiz and adjudication:** about another day.

## Design questions for the owner

1. **Coverage:** must every hypothesis word be in a clause? Proposal: no. Uncovered words are function words ("a",
   "is"), and an uncovered content word is the labeller's choice. The derivation ignores uncovered words.
2. **Rule exceptions:** disjunctions ("a dog or a cat") and quantifiers ("all", "no") break the any/all rule. Proposal: a
   `label_override` the labeller sets explicitly (with a required note), stored next to the derived label and counted
   in exports. The override rate measures how often the rule fails on our data. That is worth comparing with Stacey et
   al. 2022, once their results have been verified.
3. **Evidence granularity:** word-snapped like today, which is the E07 finding.
4. **Unaddressed with partial evidence:** allow evidence on `unaddressed` clauses as "related but insufficient"? This
   proposal says no, to keep v1 simple.

## Risks to existing data

The change is additive: a new table, new nullable columns, a new task type value. No existing row changes meaning.

| Data | Risk |
|---|---|
| **pilot** (695 items, 5 annotations), `reasons` task | None. |
| **E17** batches (`e17-esnli-50` open with 43 of the owner's annotations; three 150-item drafts) | None to the stored labels. Items are immutable once imported, and `question_json` is never rewritten. To label the same pairs as clauses, import them again under a new qid (e.g. `nli-clauses`) on the same row ids, into new batches. The reasons labels and the clause labels on the same pairs can then be compared. `score_human.py` keeps working on the existing batches and needs a branch for the new ones. |
| **Schema V9 migration** | The only non-trivial step is the `batches` rebuild for the CHECK. Migrations run on server start; each runs in one transaction, and a backup is taken at startup. Ship it only with a migration test on a copy of the live database. |
| **Live server** (127.0.0.1:8000) | Picking up the code means a restart, and the restart runs the migration. Do it between labelling sessions, with the owner's OK. |
| **Hidden gold leaking across task types** | See the table above. It must be fixed before external labellers see a clause batch. |

## Small fixes (independent, each under an hour)

- **`.question-text { white-space: pre-line; }`**: instruction line breaks render, so the E17 protocol can use them.
  Safe for existing items (only text with newlines changes).
- **`batch set-reasons NAME r1,r2,…`**: a CLI command to narrow a batch's reason set.
  - Existing annotations keep their `reasons_json`.
  - A reason removed later counts as "not asked" for new labels only.
  - Agreement already treats `None` as missing.
  - Refuse while the batch is open, or require `--force`.
