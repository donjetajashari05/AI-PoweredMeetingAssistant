"""
Duplicate check for new tasks, in two levels (thesis 4.4).

Level 1 - candidates: existing work that might be the same task.
  Sources: (a) the system's memory: open and recently closed tasks/tickets from earlier
  meetings of the same project (and earlier tasks of this meeting), and (b) the tickets
  that already exist in Jira/Trello, read through the API (many are made by hand).
  Scores:
    semantic  cosine similarity of multilingual sentence embeddings, so
              "rregullo problemin me hyrjen në sistem" finds "Fix login error";
    keyword   overlap of important words, plus ticket codes said aloud (e.g. "PAY-12").
  Only candidates above DEDUP_THRESHOLD are kept.

Level 2 - decision: the language model gets the task, its quote and the candidates
  (status, person, deadline) and picks one of: new | duplicate | update | reopen.
  Without an AI key a simple rule on the best score is used.

Nothing is changed on the platform here: the decision waits for the PM (see memory.apply_action).

.env:
  EMBEDDING_MODEL   default sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
  DEDUP_MODE        both | semantic | keyword   (for the experiments in 4.6)
  DEDUP_THRESHOLD   minimum score of a candidate (default 0.55 with embeddings, 0.35 without)
"""
import json
import math
import os
import re
import sys
import threading
import traceback
import unicodedata
from collections import Counter
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from . import db, trackers

if str(db.ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(db.ROOT / "scripts"))

import llm  # noqa: E402  (scripts/llm.py)

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MAX_CANDIDATES = 5
MEMORY_DAYS = 90  # closed work older than this is not a candidate any more
TICKET_KEY = re.compile(r"\b([A-Z][A-Z0-9_]{1,9}-\d+)\b")

STOPWORDS = set("""
the a an and or to of for in on with from by at be is are this that it as into up out new
dhe ose të te në me nga per për një nje që qe se si do kam ka kemi kanë jam është eshte
ta ia e i u o ky kjo këtë kete atë ate tjetër tjeter duhet bëj bej bëjmë
""".split())


# ---------- Text helpers ----------

def _norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def words(s):
    return {w for w in re.findall(r"[a-z0-9]+", _norm(s)) if len(w) >= 4 and w not in STOPWORDS}


def _stem(w):
    return w[:max(4, len(w) - 3)] if len(w) > 5 else w


def keyword_score(a, b):
    """Shared important words (Albanian endings ignored), 0..1."""
    wa, wb = {_stem(w) for w in words(a)}, {_stem(w) for w in words(b)}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def _trigrams(s):
    s = f"  {_norm(s)} "
    return Counter(s[i:i + 3] for i in range(len(s) - 2))


def _cosine_counts(a, b):
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na, nb = math.sqrt(sum(v * v for v in a.values())), math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


# ---------- Embeddings ----------

_model = None
_model_lock = threading.Lock()
_model_failed = False
_vec_cache = {}  # text -> normalized embedding


def embedder():
    """The multilingual sentence model, or None if sentence-transformers isn't installed."""
    global _model, _model_failed
    if _model is not None or _model_failed:
        return _model
    with _model_lock:
        if _model is None and not _model_failed:
            try:
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer(os.environ.get("EMBEDDING_MODEL") or DEFAULT_MODEL)
            except Exception:
                traceback.print_exc()
                _model_failed = True  # fall back to character n-grams
    return _model


def has_embeddings():
    return embedder() is not None


def semantic_scores(query, texts):
    """Cosine similarity of `query` against each text: embeddings if available, else character trigrams
    (the fallback only works for the same language)."""
    if not texts:
        return []
    model = embedder()
    if model is not None:
        # Ticket titles repeat for every task of a meeting: encode each text once
        missing = [t for t in dict.fromkeys([query, *texts]) if t not in _vec_cache]
        if missing:
            if len(_vec_cache) > 20000:
                _vec_cache.clear()
            _vec_cache.update(zip(missing, model.encode(missing, normalize_embeddings=True)))
        q = _vec_cache[query]
        return [float(q @ _vec_cache[t]) for t in texts]
    q = _trigrams(query)
    return [_cosine_counts(q, _trigrams(t)) for t in texts]


def mode():
    m = (os.environ.get("DEDUP_MODE") or "both").strip().lower()
    return m if m in ("both", "semantic", "keyword") else "both"


def threshold():
    try:
        return float(os.environ["DEDUP_THRESHOLD"])
    except (KeyError, ValueError):
        return 0.55 if has_embeddings() else 0.35


def score_candidates(task_text, candidates, quote=""):
    """Add 'score', 'semantic', 'keyword' to each candidate; return those above the threshold, best first."""
    if not candidates:
        return []
    said_keys = set(TICKET_KEY.findall(f"{task_text} {quote}"))
    sem = semantic_scores(task_text, [c["title"] for c in candidates]) if mode() != "keyword" else [0.0] * len(candidates)
    out = []
    for c, s in zip(candidates, sem):
        kw = keyword_score(task_text, c["title"]) if mode() != "semantic" else 0.0
        c["semantic"], c["keyword"] = round(s, 3), round(kw, 3)
        c["score"] = round(max(s, kw * 0.9), 3)
        if c.get("key") and c["key"] in said_keys:  # the ticket code was said in the meeting
            c["score"] = 1.0
        out.append(c)
    keep = [c for c in out if c["score"] >= threshold()]
    return sorted(keep, key=lambda c: -c["score"])[:MAX_CANDIDATES]


# ---------- Level 1: candidates ----------

def memory_candidates(meeting, task_id):
    """Tasks and tickets from this project's earlier meetings (open, or closed in the last MEMORY_DAYS days),
    plus the tasks listed before this one in the same meeting."""
    since = (date.fromisoformat(meeting["meeting_date"]) - timedelta(days=MEMORY_DAYS)).isoformat()
    with db.connect() as c:
        rows = db.rows(c.execute(db.TASK_SELECT + """
            WHERE t.merged_into IS NULL AND t.id != ?
              AND (t.dedup_action IS NULL OR t.dedup_action = 'new')
              AND ((m.project_id IS ? AND m.id != ? AND (m.meeting_date < ? OR (m.meeting_date = ? AND m.id < ?))
                    AND (t.status = 'open' OR COALESCE(t.completed_at, m.meeting_date) >= ?))
                   OR (t.meeting_id = ? AND t.id < ?))
            ORDER BY m.meeting_date DESC LIMIT 300
        """, (task_id, meeting["project_id"], meeting["id"], meeting["meeting_date"], meeting["meeting_date"], meeting["id"],
              since, meeting["id"], task_id)))
    return [{
        "source": "this meeting" if r["meeting_id"] == meeting["id"] else "memory",
        "task_id": r["id"], "key": r["jira_key"], "url": r["jira_url"], "title": r["description"],
        "status": r.get("tracker_status") or r["status"], "done": r["status"] == "done",
        "assignee": r["assignee_name"], "assignee_id": r["assignee_id"], "due_date": r["due_date"],
        "meeting": f"{r['meeting_title']} ({r['meeting_date']})",
    } for r in rows]


def tracker_candidates(project):
    """Existing tickets on Jira/Trello. Errors are returned, not raised: the check goes on with memory only."""
    try:
        tracker = trackers.for_project(project)
        issues = tracker.list_issues()
    except trackers.TrackerError as e:
        return [], str(e)
    return [{"source": tracker.platform, "task_id": None, "key": i.key, "url": i.url, "title": i.title,
             "status": i.status, "done": i.done, "assignee": i.assignee, "assignee_id": None, "due_date": i.due_date,
             "meeting": None} for i in issues], None


def merge_sources(memory, tracker):
    """A ticket that is both in memory and on the platform becomes one candidate (live status from the platform)."""
    by_key = {c["key"]: c for c in tracker if c["key"]}
    out = list(tracker)
    for m in memory:
        t = by_key.get(m["key"]) if m["key"] else None
        if t:
            t.update(task_id=m["task_id"], assignee_id=m["assignee_id"], meeting=m["meeting"],
                     assignee=t["assignee"] or m["assignee"], due_date=t["due_date"] or m["due_date"])
        else:
            out.append(m)
    return out


# ---------- Level 2: decision ----------

class Verdict(BaseModel):
    task: int = Field(description="Number of the new task, as given")
    action: Literal["new", "duplicate", "update", "reopen"]
    candidate: int | None = Field(description="Number of the matching existing ticket; null when action is 'new'")
    reason: str = Field(description="One short sentence in English: why")
    new_due_date: str | None = Field(description="For 'update': the new deadline YYYY-MM-DD if it changed, else null")
    new_assignee: str | None = Field(description="For 'update': the new responsible person's name if it changed, else null")


class Verdicts(BaseModel):
    items: list[Verdict]


SYSTEM_PROMPT = """You check tasks from a new work meeting against EXISTING work (tickets in Jira/Trello and tasks
from earlier meetings) so that no duplicate tickets are created. Tasks and tickets may be in different
languages (Albanian / English) and worded differently: compare the MEANING of the work.

For every new task choose exactly one action:
- new: none of its candidates is the same piece of work. Related work (same feature, different job) is NOT the same.
- duplicate: the same work is mentioned again and nothing changed (same person, same or no deadline, still open).
- update: the same work, still open, but the meeting changed its deadline or the responsible person.
  Fill new_due_date / new_assignee only with values that differ from the candidate.
- reopen: the same work, but the candidate is DONE/closed and the meeting says it is needed again
  (bug came back, not really finished, must be redone).

Be strict: when unsure whether it is the same work, choose "new" — linking a new task to an unrelated ticket
would make the new task disappear. A ticket code said in the meeting (e.g. PAY-12) is strong evidence."""


def _block(i, t, cands):
    lines = [f"New task {i}: {t['description']}",
             f"  Person: {t['assignee_name'] or '-'} | Deadline: {t['due_date'] or '-'}"
             + (f" (said: \"{t['due_date_text']}\")" if t.get("due_date_text") else ""),
             f"  Quote: \"{t['quote'] or ''}\"", "  Candidates:"]
    for j, c in enumerate(cands, 1):
        lines.append(f"    {j}. [{c.get('key') or c['source']}] {c['title']} | status: {c['status'] or '-'}"
                     f"{' (DONE)' if c['done'] else ''} | person: {c['assignee'] or '-'} | deadline: {c['due_date'] or '-'}"
                     f" | similarity {c['score']:.2f}")
    return "\n".join(lines)


def rule_verdict(task, cands):
    """Without an AI key: the best candidate above a high score decides."""
    if not cands or cands[0]["score"] < max(threshold() + 0.2, 0.75):
        return {"action": "new", "candidate": None, "reason": "No existing ticket is similar enough."}
    c = cands[0]
    if c["done"]:
        return {"action": "reopen", "candidate": 1, "reason": "The matching ticket is closed but the work came up again."}
    changes = {}
    if task["due_date"] and task["due_date"] != c["due_date"]:
        changes["new_due_date"] = task["due_date"]
    if task["assignee_id"] and c["assignee_id"] and task["assignee_id"] != c["assignee_id"]:
        changes["new_assignee"] = task["assignee_name"]
    if changes:
        return {"action": "update", "candidate": 1, "reason": "Same work with a changed deadline or person.", **changes}
    return {"action": "duplicate", "candidate": 1, "reason": "The same work is already a ticket."}


def decide(tasks_with_cands):
    """[(task, candidates)] -> {task_id: verdict dict}. Uses the language model when available."""
    out = {}
    ask = [(t, c) for t, c in tasks_with_cands if c]
    for t, c in tasks_with_cands:
        if not c:
            out[t["id"]] = {"action": "new", "candidate": None, "reason": "No similar existing work was found."}
    if not ask:
        return out
    if llm.available():
        try:
            text = "\n\n".join(_block(i, t, c) for i, (t, c) in enumerate(ask, 1))
            res = llm.structured(SYSTEM_PROMPT, f"<tasks>\n{text}\n</tasks>", Verdicts, max_tokens=6000, effort="medium")
            for v in res.items:
                if 1 <= v.task <= len(ask):
                    t, c = ask[v.task - 1]
                    if v.action != "new" and not (v.candidate and 1 <= v.candidate <= len(c)):
                        continue  # an invalid pick falls back to the rule below
                    out[t["id"]] = v.model_dump(exclude={"task"})
        except llm.LLMError:
            traceback.print_exc()
    for t, c in ask:
        out.setdefault(t["id"], rule_verdict(t, c))
    return out


# ---------- Run for a meeting ----------

def check_meeting(meeting_id):
    """Run both levels for the meeting's tasks that have not been checked or applied yet.
    Returns {"checked": n, "tracker_error": str|None}."""
    m = db.get_meeting(meeting_id)
    with db.connect() as c:
        tasks = db.rows(c.execute(db.TASK_SELECT + " WHERE t.meeting_id = ? AND t.merged_from IS NULL "
                                  "AND t.jira_key IS NULL AND t.merged_into IS NULL "
                                  "AND (t.dedup_state IS NULL OR t.dedup_state = 'pending') ORDER BY t.id", (meeting_id,)))
    if not tasks:
        return {"checked": 0, "tracker_error": None}
    project = trackers.project_info(m["project_id"])
    remote, err = tracker_candidates(project) if project or os.environ.get("JIRA_DEFAULT_PROJECT") or trackers.mock() else ([], None)

    pairs = []
    for t in tasks:
        cands = merge_sources(memory_candidates(m, t["id"]), [dict(r) for r in remote])
        pairs.append((t, score_candidates(t["description"], cands, t["quote"] or "")))
    verdicts = decide(pairs)

    with db.connect() as c:
        for t, cands in pairs:
            v = verdicts[t["id"]]
            target = cands[v["candidate"] - 1] if v.get("candidate") else None
            changes = {k: v.get(f"new_{k}") for k in ("due_date", "assignee") if v.get(f"new_{k}")}
            c.execute("""UPDATE tasks SET dedup_action = ?, dedup_target = ?, dedup_candidates = ?, dedup_reason = ?,
                         dedup_changes = ?, dedup_state = 'pending' WHERE id = ?""",
                      (v["action"], json.dumps(target, ensure_ascii=False) if target else None,
                       json.dumps(cands, ensure_ascii=False), v["reason"][:500],
                       json.dumps(changes, ensure_ascii=False) if changes else None, t["id"]))
    return {"checked": len(tasks), "tracker_error": err}
