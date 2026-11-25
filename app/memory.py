"""
Long-term project memory (literature review: long-term memory).

1) Structured memory of tasks: every change of a task (created on the platform, mentioned
   again, deadline moved, person changed, done, reopened, blocked) is one row in
   task_events, with the meeting, date and quote it came from.
2) The status is synced from Jira/Trello every SYNC_MINUTES (default 30), so the memory
   also sees changes made directly on the platform (e.g. a ticket moved to "Done").
3) Before a new meeting is analysed, the model gets the project's open work, recent
   decisions and summaries (context_for).

apply_action() carries out the duplicate-check decision once the PM confirms it.
"""
import json
import os
import threading
import time
import traceback

from . import db, trackers

SCHEMA = """
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,  -- the task/ticket in memory (may be NULL)
    tracker_key TEXT,                                        -- the ticket on Jira/Trello (may be NULL)
    meeting_id INTEGER REFERENCES meetings(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,      -- created | mentioned | due_changed | assignee_changed | done | reopened | status_changed
    old_value TEXT,
    new_value TEXT,
    quote TEXT,
    source TEXT NOT NULL DEFAULT 'meeting',  -- meeting | platform | user
    actor_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_events_task ON task_events(task_id);
CREATE INDEX IF NOT EXISTS task_events_key ON task_events(tracker_key);
"""


class ActionError(Exception):
    pass


def init():
    with db.connect() as c:
        c.executescript(SCHEMA)


def log(c, kind, task_id=None, key=None, meeting_id=None, old=None, new=None, quote=None, source="meeting", actor_id=None):
    c.execute("INSERT INTO task_events (task_id, tracker_key, meeting_id, kind, old_value, new_value, quote, source, "
              "actor_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (task_id, key, meeting_id, kind, old, new, quote, source, actor_id, db.now()))


def history(task_id=None, key=None):
    """All events of a task (by memory id and/or platform key), oldest first."""
    with db.connect() as c:
        return db.rows(c.execute("""
            SELECT e.*, m.title AS meeting_title, m.meeting_date, emp.full_name AS actor_name
            FROM task_events e LEFT JOIN meetings m ON m.id = e.meeting_id LEFT JOIN employees emp ON emp.id = e.actor_id
            WHERE (e.task_id = ? AND ? IS NOT NULL) OR (e.tracker_key = ? AND ? IS NOT NULL)
            ORDER BY e.created_at, e.id""", (task_id, task_id, key, key)))


# ---------- Applying a duplicate-check decision ----------

def _comment(task, action):
    link = f"{(os.environ.get('APP_URL') or 'http://127.0.0.1:8000').rstrip('/')}/#/meeting/{task['meeting_id']}"
    verb = {"update": "Updated", "reopen": "Reopened", "duplicate": "Mentioned again"}[action]
    lines = [f"{verb} in the meeting “{task['meeting_title']}” ({task['meeting_date']})."]
    if task.get("quote"):
        lines.append(f"Said: “{task['quote']}”")
    lines.append(f"Meeting Assistant: {link}")
    return "\n\n".join(lines)


def apply_action(task_id, action=None, candidate=None, actor_id=None):
    """Carry out the PM-confirmed decision for a task.
    action: override the AI's action (new|duplicate|update|reopen); candidate: 1-based index into the
    stored candidates to use instead of the AI's pick. Returns the updated task."""
    t = db.get_task(task_id)
    if not t:
        raise ActionError("Task not found.")
    if t["dedup_state"] == "applied":
        raise ActionError("This decision was already applied.")
    action = action or t["dedup_action"] or "new"
    if action not in ("new", "duplicate", "update", "reopen"):
        raise ActionError("Unknown action.")
    cands = json.loads(t["dedup_candidates"] or "[]")
    target = json.loads(t["dedup_target"]) if t["dedup_target"] else None
    if candidate:
        if not 1 <= candidate <= len(cands):
            raise ActionError("Unknown candidate.")
        target = cands[candidate - 1]
    changes = json.loads(t["dedup_changes"] or "{}")

    if action == "new":
        with db.connect() as c:
            c.execute("UPDATE tasks SET dedup_action = 'new', dedup_state = 'applied' WHERE id = ?", (task_id,))
        return db.get_task(task_id)
    if not target:
        raise ActionError("Choose which existing ticket this task matches.")

    if action == "update" and not changes:
        # The PM chose "update" by hand: take this task's own deadline/person when they differ
        if t["due_date"] and t["due_date"] != target.get("due_date"):
            changes["due_date"] = t["due_date"]
        if t["assignee_name"] and t["assignee_name"] != target.get("assignee"):
            changes["assignee"] = t["assignee_name"]
    assignee = None
    if changes.get("assignee"):
        from . import org
        people = org.participants(None, t["project_id"])
        emp_id = org.match_employee(changes["assignee"], people)
        emp = next((p for p in people if p["id"] == emp_id), None)
        assignee = {"name": emp["full_name"] if emp else changes["assignee"],
                    "email": emp["email"] if emp else None, "id": emp["id"] if emp else None}

    # 1) The platform (only when the target is a real ticket)
    key = target.get("key")
    if key and action in ("update", "reopen", "duplicate"):
        project = trackers.project_info(t["project_id"])
        tracker = trackers.for_project(project, employee_id=actor_id)
        comment = _comment(t, action)
        if action == "update":
            tracker.update(key, due_date=changes.get("due_date"), assignee=assignee, comment=comment)
        elif action == "reopen":
            tracker.reopen(key, comment=comment)
        # duplicate: no change on the platform, only noted in memory

    # 2) Memory
    mem_id = target.get("task_id")
    with db.connect() as c:
        if mem_id and action == "update":
            if changes.get("due_date"):
                c.execute("UPDATE tasks SET due_date = ?, due_date_text = NULL WHERE id = ?", (changes["due_date"], mem_id))
            if assignee and assignee.get("id"):
                c.execute("UPDATE tasks SET assignee_id = ?, assignee = ? WHERE id = ?",
                          (assignee["id"], assignee["name"], mem_id))
        if mem_id and action == "reopen":
            c.execute("UPDATE tasks SET status = 'open', completed_at = NULL WHERE id = ?", (mem_id,))
        ev = dict(task_id=mem_id, key=key, meeting_id=t["meeting_id"], quote=t["quote"], actor_id=actor_id)
        if action == "duplicate":
            log(c, "mentioned", **ev)
        elif action == "reopen":
            log(c, "reopened", old="done", new="open", **ev)
        else:
            if changes.get("due_date"):
                log(c, "due_changed", old=target.get("due_date"), new=changes["due_date"], **ev)
            if changes.get("assignee"):
                log(c, "assignee_changed", old=target.get("assignee"), new=assignee["name"], **ev)
            if not changes:
                log(c, "mentioned", **ev)
        # The new task is now part of the existing work, not a separate ticket
        c.execute("""UPDATE tasks SET dedup_action = ?, dedup_target = ?, dedup_state = 'applied', linked_key = ?,
                     linked_task_id = ?, approved_at = COALESCE(approved_at, ?) WHERE id = ?""",
                  (action, json.dumps(target, ensure_ascii=False), key, mem_id, db.now(), task_id))
    return db.get_task(task_id)


def dismiss(task_id):
    """The PM keeps the task as it is (no link): same as choosing 'new'."""
    return apply_action(task_id, action="new")


# ---------- Status sync with Jira / Trello ----------

def sync_statuses(limit=300):
    """Read the status of every ticket in memory from its platform. Returns {"checked", "changed", "errors"}."""
    with db.connect() as c:
        rows = db.rows(c.execute("""
            SELECT t.id, t.jira_key, t.status, t.tracker_status, t.jira_project, m.project_id
            FROM tasks t JOIN meetings m ON m.id = t.meeting_id
            WHERE t.jira_key IS NOT NULL ORDER BY t.sent_at DESC, t.id DESC LIMIT ?""", (limit,)))
    adapters, checked, changed, errors = {}, 0, 0, []
    for r in rows:
        group = (r["project_id"], r["jira_project"])
        try:
            if group not in adapters:
                adapters[group] = trackers.for_project(trackers.project_info(r["project_id"]), override_ref=r["jira_project"])
            status, done = adapters[group].status(r["jira_key"])
        except trackers.TrackerError as e:
            errors.append(f"{r['jira_key']}: {e}")
            continue
        checked += 1
        new = "done" if done else "open"
        with db.connect() as c:
            if status != r["tracker_status"]:
                c.execute("UPDATE tasks SET tracker_status = ? WHERE id = ?", (status, r["id"]))
            if new != r["status"]:
                changed += 1
                c.execute("UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?",
                          (new, db.now() if done else None, r["id"]))
                log(c, "done" if done else "reopened", task_id=r["id"], key=r["jira_key"], old=r["status"], new=new,
                    source="platform")
            elif r["tracker_status"] and status != r["tracker_status"]:
                log(c, "status_changed", task_id=r["id"], key=r["jira_key"], old=r["tracker_status"], new=status,
                    source="platform")
    return {"checked": checked, "changed": changed, "errors": errors[:20]}


def start_sync():
    minutes = max(5, int(os.environ.get("SYNC_MINUTES", "30")))

    def loop():
        while True:
            time.sleep(minutes * 60)
            try:
                if trackers.configured():
                    sync_statuses()
            except Exception:
                traceback.print_exc()

    threading.Thread(target=loop, daemon=True, name="tracker-sync").start()


# ---------- Context for the next meeting ----------

def context_for(meeting, open_limit=40, meetings_back=3):
    """Text with the project's open work, recent decisions and summaries (the newest, most relevant only)."""
    if not meeting.get("project_id"):
        return ""
    with db.connect() as c:
        open_tasks = db.rows(c.execute(db.TASK_SELECT + """
            WHERE m.project_id = ? AND m.id != ? AND m.meeting_date <= ? AND t.status = 'open'
              AND t.merged_into IS NULL AND (t.dedup_action IS NULL OR t.dedup_action = 'new')
            ORDER BY m.meeting_date DESC, t.id DESC LIMIT ?""",
            (meeting["project_id"], meeting["id"], meeting["meeting_date"], open_limit)))
        recent = db.rows(c.execute("""
            SELECT id, title, meeting_date, summary FROM meetings
            WHERE project_id = ? AND id != ? AND meeting_date <= ? AND summary IS NOT NULL
            ORDER BY meeting_date DESC, id DESC LIMIT ?""",
            (meeting["project_id"], meeting["id"], meeting["meeting_date"], meetings_back)))
        decisions = db.rows(c.execute(f"""
            SELECT d.description, m.meeting_date FROM decisions d JOIN meetings m ON m.id = d.meeting_id
            WHERE d.meeting_id IN ({db._in([r['id'] for r in recent])}) ORDER BY m.meeting_date DESC""",
            [r["id"] for r in recent]))
    parts = []
    if open_tasks:
        parts.append("Open work from earlier meetings:\n" + "\n".join(
            f"- {('[' + t['jira_key'] + '] ') if t['jira_key'] else ''}{t['description']} | {t['assignee_name'] or 'unassigned'}"
            f" | due {t['due_date'] or '-'} | from {t['meeting_date']}" for t in open_tasks))
    if decisions:
        parts.append("Recent decisions:\n" + "\n".join(f"- ({d['meeting_date']}) {d['description']}" for d in decisions))
    if recent:
        parts.append("Recent meetings:\n" + "\n".join(f"- {r['meeting_date']} {r['title']}: {r['summary']}" for r in recent))
    return "\n\n".join(parts)


# ---------- Task status for "Ask the archive" ----------

def task_sources(keywords, meeting_ids=None, limit=6):
    """Tasks whose text matches the keywords, with their current status and history."""
    from .dedup import keyword_score
    query = " ".join(keywords)
    if not query.strip():
        return []
    only = "" if meeting_ids is None else f" AND (t.meeting_id IN ({db._in(meeting_ids)}))"
    with db.connect() as c:
        rows = db.rows(c.execute(db.TASK_SELECT + " WHERE t.merged_into IS NULL" + only
                                 + " ORDER BY m.meeting_date DESC LIMIT 1000", tuple(meeting_ids or ())))
    texts = [r["description"] + " " + (r["jira_key"] or "") for r in rows]
    # Words (incl. ticket codes) or meaning: the question may be English and the task Albanian
    from .dedup import semantic_scores
    sem = semantic_scores(keywords[0], texts) if rows else []
    scored = sorted(((max(keyword_score(query, t), s - 0.16), r) for t, s, r in zip(texts, sem, rows)),
                    key=lambda x: -x[0])
    out = []
    for s, r in scored[:limit]:
        if s < 0.34:
            break
        events = history(r["id"], r["jira_key"])
        moved = sum(1 for e in events if e["kind"] == "due_changed")
        status = r["tracker_status"] or r["status"]
        text = (f"{r['description']} — status: {status}"
                + (f" ({r['jira_key']})" if r["jira_key"] else "")
                + f", person: {r['assignee_name'] or 'unassigned'}, due: {r['due_date'] or '-'}"
                + (f", deadline moved {moved} time(s)" if moved else "")
                + (f", completed {r['completed_at'][:10]}" if r["completed_at"] and r["status"] == "done" else ""))
        if events:
            text += ". History: " + "; ".join(
                f"{(e['meeting_date'] or e['created_at'][:10])} {e['kind'].replace('_', ' ')}"
                + (f" {e['old_value'] or '-'} → {e['new_value']}" if e["new_value"] else "") for e in events[-6:])
        out.append({"text": text, "kind": "task_status", "start": None, "speaker": None, "meeting_id": r["meeting_id"],
                    "meeting_title": r["meeting_title"], "meeting_date": r["meeting_date"]})
    return out
