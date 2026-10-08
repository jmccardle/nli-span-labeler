# The `clauses` labelling task

**Status:** built on branch `e13/clauses` (2026-10-08) and deployed on the owner instance. It replaces the assessment of
the same date; the owner approved the design that day.

**Readers:** labellers (sections 1–3), the MBNLIv3 research thread (sections 4–7), app maintainers (section 8).

## 1. What is labelled

An item is a premise and a hypothesis. In decider terms the **premise is the state** and the **hypothesis is the
question**. The labeller breaks the hypothesis into **clauses**: the separate conditions it claims. For each clause
they answer two questions:

1. Which premise words bear on it?
2. Do those words settle it?

| Stance | Key | Premise evidence | Meaning | Relation (E0 / FR-23) |
|---|---|---|---|---|
| `supported` | `1` / `s` | required | the evidence settles the clause true | relevant, supports |
| `contradicted` | `2` / `c` | required | the evidence settles the clause false | relevant, refutes |
| `undetermined` | `3` / `d` | required: the less specific span | related, but not settled. P "a dog", H "a poodle" | relevant, no stance |
| `unaddressed` | `4` / `u` | none allowed | nothing in the premise bears on it | irrelevant |

- **Omission** (`o`) applies to contradicted clauses only. It marks a contradiction that comes from what the premise
  leaves out, when the premise sets an exhaustive scope. P "The menu: soup, salad." H "the menu has pizza": the clause
  "has pizza" is contradicted, its evidence is the closing scope "The menu: soup, salad.", and the omission flag is set.
  Every settled clause stays tied to premise words.
- **The sentence label is derived, not entered:**
  - any clause contradicted → **contradiction**;
  - otherwise, every clause supported → **entailment**;
  - otherwise (an undetermined or unaddressed clause) → **neutral**.
- **Coverage:** not every hypothesis word has to be in a clause. Words that claim nothing ("a", "is", "the") stay out,
  and the derivation ignores them.
- **Override:** disjunctions ("a dog or a cat") and quantifiers ("all", "no") can break the any/all rule. The labeller
  can then override the label; that needs a note saying why. The derived label is kept too, so the override rate
  measures how often the rule fails.
- **Neutral completions (optional):** two sentences of at most 12 words each. "E:" would make the pair entailment if
  added to the premise; "C:" would make it contradiction.
- **Evidence granularity:** word-snapped by default (E07 finding 2); Alt-drag gives character precision.

## 2. Worked examples

| Premise | Hypothesis | Clauses | Label |
|---|---|---|---|
| A dog looks up at its loving owner. | A dog is looking up. | "dog" supported ← "dog"; "looking up" supported ← "looks up" | entailment ("loving" is in no clause) |
| A dog is looking up. | A dog looks up at its loving owner. | "dog", "looks up" supported; "its loving owner" unaddressed | neutral |
| A dog looks up at its loving owner on the beach. | A brown dog is looking up at a man. | "brown" undetermined ← "dog"; "dog", "looking up" supported; "a man" undetermined ← "owner" | neutral |
| A cat sleeps. | A dog sleeps on a red mat. | "dog" contradicted ← "cat"; "sleeps" supported; "on a red mat" unaddressed | contradiction (it wins over unaddressed) |

## 3. Keys

| Action | Keys |
|---|---|
| Select words | drag, or `←` `→` / `h` `l` to move and Shift (or `H` `L`) to extend |
| Switch between the premise and the hypothesis | `Tab` |
| Make a clause from selected hypothesis words | `1`–`4` (or `s` `c` `d` `u`) |
| Re-tag a clause | select inside it and press a stance key; with nothing selected, the key re-tags the active clause |
| Link selected premise words to the active clause | `a` |
| Change the active clause | `[` `]`, or click it |
| Toggle omission | `o` |
| Remove the active clause | `Del` |
| Remove the active clause's last evidence | `Shift+Del` |
| Write the completion sentences | `i` (Enter saves the item) |
| Note | `n` |
| Save and go to the next item | `Enter` |
| Save despite a missing-evidence warning | `Shift+Enter` |
| Skip, undo, edit an earlier item, flag | `x`, `z`, `e`, `f` |

## 4. Inference: how the model uses this (context for the research thread)

There is no recursion: it is one pass in design B.

1. Encode the premise (the state) once; it is cached.
2. Run the hypothesis through the question path, attending to the cached premise.
3. A per-hypothesis-token head outputs:
   - a **stance tag**: one of the four stances, or `none` for function words;
   - a **pointer over premise tokens**: the premise words this token rests on. This is the E10 maxsim evidence head,
     conditioned per hypothesis token.
4. The sentence label is the any/all rule over the tokens: max over contradicted, min over supported. Ordinary NLI
   labels give weak supervision through that rule (cf. Stacey et al. 2022, still to be verified); clause labels
   supervise the tags directly.

**Clauses are not a separate extraction step.** A clause is a run of neighbouring tokens with the same tag and the same
evidence. Clause boundaries mostly follow the hypothesis's own grammar. How fine a split needs to be can depend on the
premise ("on the beach" against "on sand"), and the question path sees the premise anyway.

**Abstention signal:** a neutral item whose open clauses are all `undetermined` (relevant but too vague) differs from
one with an `unaddressed` clause (irrelevant). That is relevance × stance, per clause.

**Certification:** the E17 harness can probe each clause against its own evidence. That is the place for the
recursive, per-clause re-ask, not serving.

## 5. Item format (pool rows)

Pool rows are as before (requirements §5.2), plus a labeller-visible `question.hypothesis` string. The question stays
`choice` (entailment / neutral / contradiction), so the API contract's question types are unchanged.

```json
{"id": "e17h-…", "source": "snli", "split": "test", "state": "<premise>", "state_format": "text",
 "questions": {"clauses": {"type": "choice", "instructions": "<protocol; line breaks are kept>",
   "criteria": {"entailment": "…", "neutral": "…", "contradiction": "…"},
   "hypothesis": "<hypothesis>"}},
 "e13": {"…": "hidden from labellers"}}
```

```
python -m e13_labeler import FILE --batch NAME --task clauses
```

- `--task clauses` creates a clauses batch: no reason set, no span policy.
- A clauses batch rejects rows without `question.hypothesis`.
- Importing into an existing batch requires the task type to match.

## 6. Exports

**`clauses.jsonl`** (export kind `clauses`, included by default once a clauses batch exists). One row per (item,
labeller, batch), at the latest version, skips left out.

| Field | Content |
|---|---|
| `id`, `qid`, `item_id`, `source`, `split`, `permissions`, `state_sha256` | the item (`id` is the pool row id) |
| `batch`, `relabel_of`, `labeler`, `labeler_kind`, `version`, `gold_probe` | provenance |
| `label`, `label_derived`, `label_override` (bool) | the sentence label |
| `clauses[]` | each clause: `start`, `end`, `text`, `words`, `stance`, `omission`, `note`, and `evidence[]` (`start`, `end`, `text`, `words`, `renderer`) |
| `hypothesis_word_tags` | one entry per `hypothesis.split()` word: the stance of the clause covering it, or `null` |
| `hypothesis_word_evidence` | one entry per hypothesis word: the sorted `premise.split()` indices its clause rests on |
| `completion` | `{"entail", "contradict"}` or `null` |
| `note`, `policy_override`, `timing`, `created_at` | as in the annotations export |
| `premise`, `hypothesis` | only when text is included |

- Clause offsets index the hypothesis.
- Evidence offsets index the premise's canonical rendering (API_CONTRACT rule 7, renderer `r1`). For text premises,
  which is all NLI data, the rendering is the premise itself.
- `words` are indices into `text.split()`, the E17 harness's word indexing. They are derived at export, never stored.

**`annotations.jsonl`** gains `task`, `label`, `label_derived`, `clauses` (the stored form, without `words`) and
`completion`. For a clauses record, `answerable` and `reasons` are null.

**Not included:** `training.jsonl` (reasons targets), reasons α and adjudication all leave clause records out.

**`agreement.json`** gains `clauses`, computed over hypothesis words:
- `label`: nominal α on the sentence label;
- `word_stance`: nominal α on each hypothesis word's stance (`none` when outside a clause);
- `evidence_f1`: mean F1 between two labellers' premise word sets, over hypothesis words both linked to evidence.

These are reported inter-rater (first passes) and intra-rater (re-label batches), with label counts and the number of
overrides. Clauses themselves are never compared, because labellers segment differently.

## 7. Data shape for training

To get per-token targets for the head in section 4, map each hypothesis word's tag and its evidence word set to tokens
with the tokenizer's offsets. `render_state.token_labels` already does this for premise word sets; hypothesis word
tags follow the same word → token rule.

| Count | Classes |
|---|---|
| Five tag classes | `supported`, `contradicted`, `undetermined`, `unaddressed`, `none` |
| Three sentence labels | entailment, neutral, contradiction (`label_derived` and `label`) |
| One flag | `omission` |

## 8. App internals

| Area | Implementation |
|---|---|
| Schema V9 | Table `clauses` (`annotation_id`, `idx` in hypothesis order, offsets, `text`, `stance`, `omission`, `note`); table `clause_evidence` (`clause_id`, offsets into the rendering, `text`, `renderer`). Both have no-delete triggers. `annotations` gains `label`, `label_derived` and `completion_json`. `batches` was rebuilt to allow `task_type = 'clauses'`. |
| API | `POST /api/annotations` and `PUT /api/annotations/{id}` take `clauses[]`, `label_override` and `completion` in a clauses batch, and refuse reasons, answerable and spans there. Edits are new versions, as before. |
| Validation | `e13_labeler/clauses.py`. **Errors:** no clause; a bad or untrimmed slice; overlapping clauses; omission on a clause that isn't contradicted; evidence on an unaddressed clause; duplicate evidence; an override without a note; completion sentences on a non-neutral item. **Overridable with Shift+Enter:** a supported, contradicted or undetermined clause with no evidence (world knowledge); a completion sentence over 12 words. |
| Hidden gold | Served only in reasons batches; gold is reasons gold. |
| Not built | Clause gold, the clause quiz, clause adjudication, and model pseudo-labels for clauses. Clause batches are fine for the owner, and for trusted labellers who have passed the reasons onboarding. Build those before opening clause batches to new labellers. |

## Small fixes shipped with this

- **Instructions keep their line breaks** (`white-space: pre-line`).
- **`python -m e13_labeler batch set-reasons NAME r1,r2 [--force]`** narrows a reasons batch's reason set. Existing
  annotations keep their values, and a reason dropped from the set counts as "not asked" for new labels.
- **The page and its scripts revalidate on every load** (`Cache-Control: no-cache`).
