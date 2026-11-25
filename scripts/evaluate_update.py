"""
Evaluate level 2 of the duplicate check: new / duplicate / update / reopen
(criterion 2: duplicates avoided, and criterion 3: accuracy of updating existing tasks).

Input: a JSON list of annotated tasks from later meetings, e.g. data/eval/update_cases.json:
  [
    {"task": {"description": "Fix login error", "assignee": "Arta", "due_date": "2026-12-04",
              "due_date_text": "deri të premten tjetër", "quote": "login-in e shtyjmë për javën tjetër"},
     "candidates": [{"key": "PAY-12", "title": "Fix login error", "status": "In Progress", "done": false,
                     "assignee": "Arta", "due_date": "2026-11-27"}],
     "expected": {"action": "update", "key": "PAY-12", "new_due_date": "2026-12-04"}}
  ]
  action is one of new | duplicate | update | reopen; key is null for "new".

Each task goes through the same two levels as in the app: candidates above the similarity threshold
(level 1), then the decision (level 2) by the language model, or by the simple rule with --rule or
without an AI key.

Reported:
  - accuracy of the action and the confusion matrix (expected x predicted);
  - correct link: the action AND the existing ticket are right;
  - duplicates avoided: of the tasks that already exist (duplicate/update/reopen), how many were linked to
    the right ticket instead of creating a new one; and new tasks wrongly linked (work that would be lost);
  - updates: of the "update" cases, how many change exactly the right fields (deadline / person) with the right values.

Run from the project root:
    python scripts/evaluate_update.py data/eval/update_cases.json
    python scripts/evaluate_update.py data/eval/update_cases.json --rule     # baseline without the language model
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import dedup  # noqa: E402

ACTIONS = ["new", "duplicate", "update", "reopen"]


def person_id(name):
    return dedup._norm(name or "").strip() or None


def same_person(a, b):
    """Both empty, or one name's words are contained in the other ("Arta" ~ "Arta Krasniqi")."""
    x, y = set((person_id(a) or "").split()), set((person_id(b) or "").split())
    return (not x and not y) or (bool(x) and bool(y) and (x <= y or y <= x))


def task_row(i, t):
    """The fields dedup.decide() reads from a task in the database."""
    return {"id": i, "description": t["description"], "assignee_name": t.get("assignee"),
            "assignee_id": person_id(t.get("assignee")), "due_date": t.get("due_date"),
            "due_date_text": t.get("due_date_text"), "quote": t.get("quote", "")}


def candidate_row(c):
    return {"key": c["key"], "source": "eval", "title": c["title"], "status": c.get("status", ""),
            "done": bool(c.get("done")), "assignee": c.get("assignee"),
            "assignee_id": person_id(c.get("assignee")), "due_date": c.get("due_date")}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Evaluate new/duplicate/update/reopen decisions")
    ap.add_argument("file")
    ap.add_argument("--rule", action="store_true", help="use the simple rule instead of the language model")
    args = ap.parse_args()
    items = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if args.rule:
        dedup.llm.available = lambda: False
    decider = "language model (" + dedup.llm.label() + ")" if dedup.llm.available() else "rule (no language model)"
    print(f"{len(items)} annotated tasks · decision: {decider} · threshold {dedup.threshold():.2f} · "
          f"embeddings: {'yes' if dedup.has_embeddings() else 'NO (character n-grams)'}\n")

    pairs = []
    for i, it in enumerate(items):
        cands = dedup.score_candidates(it["task"]["description"], [candidate_row(c) for c in it["candidates"]],
                                       it["task"].get("quote", ""))
        pairs.append((task_row(i, it["task"]), cands))
    verdicts = dedup.decide(pairs)

    confusion = Counter()
    action_ok = link_ok = 0
    existing = avoided = lost_new = 0
    upd_n = upd_ok = 0
    for i, (it, (_, cands)) in enumerate(zip(items, pairs)):
        exp, v = it["expected"], verdicts[i]
        key = cands[v["candidate"] - 1]["key"] if v.get("candidate") else None
        confusion[(exp["action"], v["action"])] += 1
        a_ok = v["action"] == exp["action"]
        l_ok = a_ok and key == exp.get("key")
        action_ok += a_ok
        link_ok += l_ok
        if exp["action"] != "new":
            existing += 1
            avoided += v["action"] != "new" and key == exp.get("key")
        elif v["action"] != "new":
            lost_new += 1
        fields_note = ""
        if exp["action"] == "update":
            upd_n += 1
            ok = (l_ok and (v.get("new_due_date") or None) == (exp.get("new_due_date") or None)
                  and same_person(v.get("new_assignee"), exp.get("new_assignee")))
            upd_ok += ok
            if not ok:
                fields_note = (f"  [changes: due {v.get('new_due_date')} / person {v.get('new_assignee')}, expected "
                               f"due {exp.get('new_due_date')} / person {exp.get('new_assignee')}]")
        mark = "OK " if l_ok else "ERR"
        print(f"{mark} {it['task']['description'][:60]:60}  expected {exp['action']:9} {exp.get('key') or '-':8} "
              f"got {v['action']:9} {key or '-':8}{fields_note}")
        if not l_ok:
            print(f"      reason: {v.get('reason', '')}")

    n = len(items)
    print("\nConfusion matrix (rows = expected, columns = predicted)")
    print(" " * 11 + "".join(f"{a:>11}" for a in ACTIONS))
    for e in ACTIONS:
        print(f"{e:>11}" + "".join(f"{confusion[(e, p)]:11d}" for p in ACTIONS))

    print("\nResults")
    print(f"  Action correct:           {action_ok}/{n} ({action_ok / n:.0%})")
    print(f"  Action + ticket correct:  {link_ok}/{n} ({link_ok / n:.0%})")
    if existing:
        print(f"  Duplicates avoided:       {avoided}/{existing} ({avoided / existing:.0%}) "
              f"of the tasks that already existed (baseline that always creates: 0/{existing})")
    print(f"  New tasks wrongly linked: {lost_new}/{n - existing}")
    if upd_n:
        print(f"  Updates fully correct:    {upd_ok}/{upd_n} ({upd_ok / upd_n:.0%}) (right ticket, deadline and person)")
    for a in ACTIONS:
        tp = confusion[(a, a)]
        pred = sum(confusion[(e, a)] for e in ACTIONS)
        gold = sum(confusion[(a, p)] for p in ACTIONS)
        if gold or pred:
            p = tp / pred if pred else 0.0
            r = tp / gold if gold else 0.0
            f = 2 * p * r / (p + r) if p + r else 0.0
            print(f"    {a:9} P={p:.2f} R={r:.2f} F1={f:.2f}  (n={gold})")


if __name__ == "__main__":
    main()
