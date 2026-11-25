"""
Evaluate task and decision extraction (criterion 1: accuracy of extracting tasks and decisions),
and how much speech-recognition errors hurt it (literature review: transcription).

Input: a JSON list of manually annotated meetings, e.g. data/eval/extract_gold.json:
  [
    {"transcript": "data/samples/mbledhja_shembull.json",   # output of transcribe.py, or a .txt
     "date": "2026-11-23",                                   # meeting date (resolves "deri të premten")
     "pred": "output/mbledhja_shembull_notes.json",          # optional: notes already made by extract.py
     "decisions": ["Reduce the marketing budget by 10%"],
     "tasks": [{"description": "Prepare the final budget report", "assignee": "Arta", "due_date": "2026-11-27"}]}
  ]

Effect of transcription errors: give several transcripts of the SAME meeting, by label, e.g.
    "transcript": {"manual": "data/reference/mbledhja1.txt",
                   "large-v3-turbo": "output/turbo/mbledhja1.json",
                   "albanian-tuned": "output/tuned/mbledhja1.json"}
("pred" can be a dict with the same labels). Every transcript is scored against the same annotations,
so the drop from "manual" (no recognition errors) to each model shows what the remaining errors cost.

With --run the transcript is sent to the language model (needs ANTHROPIC_API_KEY or GROQ_API_KEY);
otherwise the "pred" file is read. Predicted and annotated items are paired one to one by meaning
(the same multilingual similarity used by the duplicate check), so wording does not have to match.

Reported per transcript label: precision, recall and F1 for tasks and for decisions; for correctly
found tasks, how often the person responsible and the deadline are right; and the missed / invented items.

Run from the project root:
    python scripts/evaluate_extract.py data/eval/extract_gold.json --run
    python scripts/evaluate_extract.py data/eval/extract_gold.json            # uses "pred" files
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import dedup  # noqa: E402

COUNTS = ("t_tp", "t_pred", "t_gold", "d_tp", "d_pred", "d_gold", "who_ok", "who_n", "due_ok", "due_n")


def similarity(a, b):
    sem = dedup.semantic_scores(a, [b])[0]
    return max(sem, dedup.keyword_score(a, b) * 0.9)


def pair(gold, pred, min_score):
    """Greedy one-to-one pairing, best similarity first. Returns [(gold_i, pred_j, score)]."""
    scores = sorted(((similarity(g, p), i, j) for i, g in enumerate(gold) for j, p in enumerate(pred)), reverse=True)
    used_g, used_p, out = set(), set(), []
    for s, i, j in scores:
        if s < min_score:
            break
        if i not in used_g and j not in used_p:
            used_g.add(i)
            used_p.add(j)
            out.append((i, j, s))
    return out


def same_person(gold, pred):
    """Names match if both are empty, or one name's words are contained in the other ("Arta" ~ "Arta Krasniqi")."""
    g, p = set(dedup._norm(gold or "").split()), set(dedup._norm(pred or "").split())
    if not g or not p:
        return not g and not p
    return g <= p or p <= g


def prf(tp, n_pred, n_gold):
    prec = tp / n_pred if n_pred else (1.0 if not n_gold else 0.0)
    rec = tp / n_gold if n_gold else 1.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return prec, rec, f1


def variants(item):
    """[(label, transcript path, pred path or None)]: one per transcript of this meeting."""
    t, pr = item["transcript"], item.get("pred")
    if isinstance(t, str):
        return [("transcript", t, pr)]
    return [(label, path, pr.get(label) if isinstance(pr, dict) else None) for label, path in t.items()]


def transcript_text(path):
    import extract
    if path.suffix.lower() == ".txt":  # a manual reference: plain text
        return path.read_text(encoding="utf-8")
    return extract.load_transcript(path)[0]


def predict(item, path, pred, run):
    if run:
        import extract
        import llm
        if not llm.available():
            raise SystemExit(llm.NO_KEY)
        notes = extract.extract(transcript_text(ROOT / path), date.fromisoformat(item["date"])).model_dump()
    elif pred:
        notes = json.loads((ROOT / pred).read_text(encoding="utf-8"))
    else:
        raise SystemExit(f'{path}: no "pred" file; run with --run to extract it now.')
    return [d["description"] for d in notes.get("decisions", [])], notes.get("tasks", [])


def score(item, p_dec, p_tasks, tot, min_score):
    g_dec, g_tasks = item.get("decisions", []), item.get("tasks", [])
    d_pairs = pair(g_dec, p_dec, min_score)
    t_pairs = pair([t["description"] for t in g_tasks], [t["description"] for t in p_tasks], min_score)
    tot["d_tp"] += len(d_pairs)
    tot["d_pred"] += len(p_dec)
    tot["d_gold"] += len(g_dec)
    tot["t_tp"] += len(t_pairs)
    tot["t_pred"] += len(p_tasks)
    tot["t_gold"] += len(g_tasks)
    for i, j, s in t_pairs:
        g, p = g_tasks[i], p_tasks[j]
        who = same_person(g.get("assignee"), p.get("assignee"))
        due = (g.get("due_date") or None) == (p.get("due_date") or None)
        tot["who_n"] += 1
        tot["who_ok"] += who
        tot["due_n"] += 1
        tot["due_ok"] += due
        notes = ""
        if not who:
            notes += f"  [person: {p.get('assignee')!r}, expected {g.get('assignee')!r}]"
        if not due:
            notes += f"  [deadline: {p.get('due_date')}, expected {g.get('due_date')}]"
        print(f"  task  OK  {s:.2f}  {g['description']}  ->  {p['description']}{notes}")
    for i in sorted(set(range(len(g_tasks))) - {i for i, _, _ in t_pairs}):
        print(f"  task  MISSED    {g_tasks[i]['description']}")
    for j in sorted(set(range(len(p_tasks))) - {j for _, j, _ in t_pairs}):
        print(f"  task  EXTRA     {p_tasks[j]['description']}")
    for i in sorted(set(range(len(g_dec))) - {i for i, _, _ in d_pairs}):
        print(f"  decision MISSED {g_dec[i]}")
    for j in sorted(set(range(len(p_dec))) - {j for _, j, _ in d_pairs}):
        print(f"  decision EXTRA  {p_dec[j]}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Evaluate task and decision extraction")
    ap.add_argument("file")
    ap.add_argument("--run", action="store_true", help="extract now with the language model")
    ap.add_argument("--match", type=float, default=None,
                    help="minimum similarity to count a prediction as the annotated item "
                         "(default 0.5 with embeddings, 0.3 without)")
    args = ap.parse_args()
    items = json.loads(Path(args.file).read_text(encoding="utf-8"))
    min_score = args.match if args.match is not None else (0.5 if dedup.has_embeddings() else 0.3)
    print(f"{len(items)} annotated meetings · pairing threshold {min_score:.2f} · "
          f"embeddings: {'yes' if dedup.has_embeddings() else 'NO (character n-grams)'}\n")

    totals = {}  # transcript label -> counts
    for item in items:
        for label, path, pred in variants(item):
            tot = totals.setdefault(label, dict.fromkeys(COUNTS, 0))
            print(f"== {path}  [{label}]")
            p_dec, p_tasks = predict(item, path, pred, args.run)
            score(item, p_dec, p_tasks, tot, min_score)
            print()

    print("Results")
    print(f"  {'transcript':16} {'tasks P':>8} {'R':>5} {'F1':>5}   {'decisions P':>11} {'R':>5} {'F1':>5}"
          f"   {'person':>7} {'deadline':>8}")
    for label, t in totals.items():
        tp, tr, tf = prf(t["t_tp"], t["t_pred"], t["t_gold"])
        dp, dr, df = prf(t["d_tp"], t["d_pred"], t["d_gold"])
        who = f"{t['who_ok'] / t['who_n']:.0%}" if t["who_n"] else "-"
        due = f"{t['due_ok'] / t['due_n']:.0%}" if t["due_n"] else "-"
        print(f"  {label:16} {tp:8.2f} {tr:5.2f} {tf:5.2f}   {dp:11.2f} {dr:5.2f} {df:5.2f}   {who:>7} {due:>8}")
    print("\n  person / deadline: share of the correctly found tasks that have the right person / deadline")


if __name__ == "__main__":
    main()
