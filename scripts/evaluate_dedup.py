"""
Evaluate level 1 of the duplicate check (thesis 4.6, criterion 2).

Input: a JSON list of annotated tasks, e.g. data/eval/dedup_pairs.json:
  [
    {"task": "Rregullo problemin me hyrjen në sistem", "quote": "",
     "lang": "cross",                       # same | cross (task and ticket in different languages)
     "candidates": [{"key": "PAY-12", "title": "Fix login error"},
                    {"key": "PAY-13", "title": "Add export to CSV"}],
     "match": "PAY-12"}                     # the existing ticket it is, or null if it is new work
  ]

For each configuration (keyword only, semantic only, both) and each threshold, the best
candidate above the threshold is the prediction. Reported: precision, recall and F1 of
duplicate detection, duplicates avoided compared with the baseline that creates a ticket
for every task, and wrong links (a new task linked to an unrelated ticket), split by same/cross language.

Run from the project root:
    python scripts/evaluate_dedup.py data/eval/dedup_pairs.json
    python scripts/evaluate_dedup.py data/eval/dedup_pairs.json --thresholds 0.3 0.9 0.05
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import dedup  # noqa: E402


def predict(item, threshold):
    os.environ["DEDUP_THRESHOLD"] = str(threshold)
    cands = [dict(c, source="eval", done=False, status="") for c in item["candidates"]]
    best = dedup.score_candidates(item["task"], cands, item.get("quote", ""))
    return best[0]["key"] if best else None


def metrics(items, preds):
    tp = sum(1 for it, p in zip(items, preds) if it["match"] and p == it["match"])
    fp = sum(1 for it, p in zip(items, preds) if p and p != it["match"])
    fn = sum(1 for it, p in zip(items, preds) if it["match"] and p != it["match"])
    wrong_new = sum(1 for it, p in zip(items, preds) if not it["match"] and p)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    baseline = sum(1 for it in items if it["match"])  # duplicates the "always create" baseline makes
    return {"n": len(items), "P": prec, "R": rec, "F1": f1, "avoided": tp, "baseline_dups": baseline,
            "left_dups": baseline - tp, "wrong_links": fp, "new_lost": wrong_new}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Evaluate the duplicate check")
    ap.add_argument("file")
    ap.add_argument("--thresholds", nargs=3, type=float, default=[0.30, 0.90, 0.05], metavar=("FROM", "TO", "STEP"))
    args = ap.parse_args()
    items = json.loads(Path(args.file).read_text(encoding="utf-8"))
    print(f"{len(items)} annotated tasks · embeddings: {'yes' if dedup.has_embeddings() else 'NO (character n-grams)'}\n")

    lo, hi, step = args.thresholds
    thresholds = [round(lo + i * step, 3) for i in range(int(round((hi - lo) / step)) + 1)]
    best = {}
    print(f"{'mode':9} {'thr':>5} {'subset':6} {'n':>4} {'P':>6} {'R':>6} {'F1':>6} {'avoided':>8} {'wrong':>6}")
    for mode in ("keyword", "semantic", "both"):
        os.environ["DEDUP_MODE"] = mode
        for thr in thresholds:
            preds = [predict(it, thr) for it in items]
            for subset in ("all", "same", "cross"):
                idx = [i for i, it in enumerate(items) if subset == "all" or it.get("lang", "same") == subset]
                if not idx:
                    continue
                r = metrics([items[i] for i in idx], [preds[i] for i in idx])
                print(f"{mode:9} {thr:5.2f} {subset:6} {r['n']:4d} {r['P']:6.2f} {r['R']:6.2f} {r['F1']:6.2f} "
                      f"{r['avoided']:4d}/{r['baseline_dups']:<3d} {r['wrong_links']:6d}")
                if subset == "all" and r["F1"] > best.get(mode, (None, {"F1": -1}))[1]["F1"]:
                    best[mode] = (thr, r)
        print()
    print("Best threshold per configuration (by F1 on all tasks):")
    for mode, (thr, r) in best.items():
        print(f"  {mode:9} threshold {thr:.2f}: P={r['P']:.2f} R={r['R']:.2f} F1={r['F1']:.2f}, "
              f"duplicates avoided {r['avoided']} of {r['baseline_dups']}, wrong links {r['wrong_links']}")


if __name__ == "__main__":
    main()
