"""
Agreement for the `clauses` task (docs/e13/CLAUSE_TASK.md). Standard library only.

Labelers split a hypothesis into clauses differently, so clauses themselves are
never compared. Units are hypothesis words, which is also what the
per-hypothesis-token head is trained on:

- label: nominal α on the sentence label, one unit per item;
- word stance: nominal α on the stance of the clause covering each hypothesis
  word ("none" when the word is in no clause), one unit per (item, word);
- evidence: for each pair of labelers and each hypothesis word both linked to
  premise evidence, the F1 between the two sets of premise words; the mean.

Inter-rater over first passes; intra-rater over re-label passes against the same
labeler's first pass.
"""

from collections import defaultdict
from itertools import combinations
from typing import Mapping, Sequence

from .agreement import alpha_nominal, bootstrap_ci
from .clauses import hypothesis_word_evidence, hypothesis_word_tags


def _alpha(units: Sequence, n_boot: int, seed: int) -> dict:
    pairable = [u for u in units if len(u) >= 2]
    return {"alpha": alpha_nominal(pairable), "ci95": bootstrap_ci(pairable, n_boot=n_boot, seed=seed),
            "n_units": len(pairable), "n_values": sum(len(u) for u in pairable)}


def _f1(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    inter = len(a & b)
    return 2 * inter / (len(a) + len(b)) if inter else 0.0


def _section(groups: Mapping, texts: Mapping, n_boot: int, seed: int) -> dict:
    """``groups``: unit key -> records (two or more to be pairable); ``texts``: item -> (hypothesis, premise)."""
    labels, word_units, f1s = [], [], []
    for recs in groups.values():
        if len(recs) < 2:
            continue
        hypothesis, premise = texts[recs[0]["item_id"]]
        labels.append([r["label"] for r in recs])
        tags = [[t or "none" for t in hypothesis_word_tags(hypothesis, r["clauses"])] for r in recs]
        word_units += [list(col) for col in zip(*tags)]
        evidence = [hypothesis_word_evidence(hypothesis, premise, r["clauses"]) for r in recs]
        for a, b in combinations(evidence, 2):
            for wa, wb in zip(a, b):
                if wa and wb:
                    f1s.append(_f1(set(wa), set(wb)))
    return {
        "label": _alpha(labels, n_boot, seed),
        "word_stance": _alpha(word_units, n_boot, seed),
        "evidence_f1": {"mean": sum(f1s) / len(f1s) if f1s else None, "n_word_pairs": len(f1s)},
        "n_items": len(labels),
    }


def clause_agreement(records: Sequence[Mapping], texts: Mapping, n_boot: int = 1000, seed: int = 0) -> dict:
    """
    ``records``: records.load_annotations rows of clauses batches; ``texts``:
    item_id -> (hypothesis, rendered premise). Human, non-skipped, non-probe only.
    """
    usable = [r for r in records if r["task"] == "clauses" and r["labeler_kind"] == "human"
              and r["skipped"] is None and not r.get("_gold_probe") and r["label"]]
    first = [r for r in usable if r.get("_relabel_of") is None]
    inter = defaultdict(list)
    for r in first:
        inter[r["item_id"]].append(r)
    by_key = {(r["batch"], r["item_id"], r["labeler"]): r for r in first}
    intra = {}
    for r in usable:
        original = by_key.get((r.get("_relabel_of"), r["item_id"], r["labeler"])) if r.get("_relabel_of") else None
        if original:
            intra[(r["item_id"], r["labeler"])] = [original, r]
    labels = defaultdict(int)
    for r in first:
        labels[r["label"]] += 1
    return {
        "inter_rater": _section(inter, texts, n_boot, seed),
        "intra_rater": _section(intra, texts, n_boot, seed),
        "label_counts": dict(sorted(labels.items())),
        "n_annotations": len(first),
        "overrides": sum(1 for r in first if r["label"] != r["label_derived"]),
    }
