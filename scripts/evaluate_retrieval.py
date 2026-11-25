"""
Evaluate "Ask the archive" (criterion 4: accuracy of finding the relevant information in past meetings).

Uses the meetings already saved in the app (data/app.db, or MEETING_AGENT_DB).
Input: a JSON list of questions with the meetings that hold the answer, e.g. data/eval/retrieval_questions.json:
  [
    {"question": "Çfarë u vendos për buxhetin e marketingut?",
     "meetings": ["Weekly sync"],              # titles (or ids) of the meeting(s) that answer it
     "facts": ["10"]}                           # optional: words the answer must contain (with --answer)
  ]

Configurations compared (as in the literature review: BM25 vs dense retrieval, DPR):
  bm25      word matching with the question's own words (baseline)
  semantic  dense retrieval: multilingual embeddings of the question vs every passage
  hybrid    both merged by Reciprocal Rank Fusion
  plan      the app's full search: the language model adds Albanian + English keywords to the hybrid
            search, and the tasks' current status from the structured memory is added (needs an AI key)

Reported per configuration: Hit@k (a right meeting among the first k sources), Recall@k (share of the
right meetings found) and MRR (how high the first right source is). With --answer the model also
writes the answer and the share of expected facts it contains is reported.

Run from the project root:
    python scripts/evaluate_retrieval.py data/eval/retrieval_questions.json
    python scripts/evaluate_retrieval.py data/eval/retrieval_questions.json --answer
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import assistant, db, dedup  # noqa: E402


def meeting_ids(names):
    """Titles or ids -> set of meeting ids."""
    with db.connect() as c:
        rows = c.execute("SELECT id, title FROM meetings").fetchall()
    out, missing = set(), []
    for n in names:
        hit = [r[0] for r in rows if str(n) == str(r[0]) or dedup._norm(str(n)) == dedup._norm(r[1] or "")]
        if hit:
            out.update(hit)
        else:
            missing.append(n)
    return out, missing


def scores(ranked, relevant, k):
    first = next((i for i, m in enumerate(ranked, 1) if m in relevant), None)
    top = set(ranked[:k])
    return {"hit": float(bool(top & relevant)), "recall": len(top & relevant) / len(relevant),
            "rr": 1 / first if first else 0.0}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Evaluate search in past meetings")
    ap.add_argument("file")
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("--answer", action="store_true", help="also generate the answers and check the facts")
    args = ap.parse_args()
    items = json.loads(Path(args.file).read_text(encoding="utf-8"))
    db.init()
    has_key = assistant.llm.available()
    configs = ["bm25"] + (["semantic", "hybrid"] if dedup.has_embeddings() else []) + (["plan"] if has_key else [])
    if not has_key:
        print("No AI key: the 'plan' configuration (keywords from the language model) is skipped.")
    if not dedup.has_embeddings():
        print("sentence-transformers is not installed: only bm25 is evaluated.")
    print()

    totals = {c: {"hit": 0.0, "recall": 0.0, "rr": 0.0} for c in configs}
    facts_ok = facts_n = 0
    used = 0
    for it in items:
        relevant, missing = meeting_ids(it["meetings"])
        if missing:
            print(f"SKIP {it['question']}: meeting not found in the database: {missing}")
            continue
        used += 1
        print(f"Q: {it['question']}")
        for cfg in configs:
            if cfg == "plan":
                ranked = [s["meeting_id"] for s in assistant.gather(it["question"], assistant.plan(it["question"]))]
            else:
                ranked = [s["meeting_id"] for s in assistant.search(it["question"], it["question"], mode=cfg)]
            r = scores(ranked, relevant, args.k)
            for m in r:
                totals[cfg][m] += r[m]
            print(f"   {cfg:8} hit@{args.k}={r['hit']:.0f} recall@{args.k}={r['recall']:.2f} RR={r['rr']:.2f}"
                  f"   sources from meetings {ranked[:args.k]} (expected {sorted(relevant)})")
        if args.answer and has_key and it.get("facts"):
            ans = assistant.ask(it["question"], True)["answer"]
            got = [f for f in it["facts"] if dedup._norm(f) in dedup._norm(ans)]
            facts_ok += len(got)
            facts_n += len(it["facts"])
            print(f"   answer: {ans[:300]}")
            print(f"   facts found: {len(got)}/{len(it['facts'])}")

    if not used:
        raise SystemExit("No question could be evaluated: add the meetings to the app first.")
    print(f"\nResults ({used} questions, k={args.k})")
    for cfg in configs:
        t = totals[cfg]
        print(f"  {cfg:8} Hit@{args.k}={t['hit'] / used:.2f}  Recall@{args.k}={t['recall'] / used:.2f}  MRR={t['rr'] / used:.2f}")
    if facts_n:
        print(f"  Answers contain {facts_ok}/{facts_n} expected facts ({facts_ok / facts_n:.0%})")


if __name__ == "__main__":
    main()
