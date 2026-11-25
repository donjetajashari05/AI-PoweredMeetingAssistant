"""
Emails to project managers:
  - after a meeting of their project is processed: summary, decisions, tasks
  - a daily reminder (once a day, from REMINDER_HOUR, default 8): tasks due
    tomorrow or overdue in their projects
"""
import os
import threading
import time
import traceback
from datetime import date, datetime, timedelta

from . import db, mailer

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminder_log (
    pm_id INTEGER NOT NULL,
    sent_on TEXT NOT NULL,
    PRIMARY KEY (pm_id, sent_on)
);
"""


def init():
    with db.connect() as c:
        c.executescript(SCHEMA)


def _pm_of_meeting(c, meeting):
    if not meeting.get("project_id"):
        return None, None
    row = c.execute("""
        SELECT p.name AS project, e.id, e.full_name, e.email FROM projects p
        JOIN employees e ON e.id = p.pm_id WHERE p.id = ? AND e.active = 1 AND e.email IS NOT NULL
    """, (meeting["project_id"],)).fetchone()
    return (dict(row), row["project"]) if row else (None, None)


def meeting_processed(meeting_id):
    """Send the meeting summary to the project's PM (once per processing)."""
    m = db.meeting_detail(meeting_id)
    if not m:
        return
    with db.connect() as c:
        pm, project = _pm_of_meeting(c, m)
    if not pm:
        return
    lines = [f"Hi {pm['full_name'].split()[0]},",
             f"The meeting “{m['title']}” ({m['meeting_date']}) of the {project} project has been processed.",
             "SUMMARY\n" + (m["summary"] or "—")]
    if m["decisions"]:
        lines.append("DECISIONS\n" + "\n".join(f"• {d['description']}" for d in m["decisions"]))
    if m["tasks"]:
        lines.append("TASKS\n" + "\n".join(
            f"• {t['description']} — {t['assignee_name'] or 'unassigned'}"
            + (f", due {t['due_date']}" if t["due_date"] else "") for t in m["tasks"]))
        lines.append("Open the meeting, tick the action items that belong together and click “Make ticket”. Nothing changes in Jira or Trello until you confirm it.")
    if m["open_questions"]:
        lines.append("OPEN QUESTIONS\n" + "\n".join(f"• {q}" for q in m["open_questions"]))
    mailer.send(pm["email"], f"Meeting processed: {m['title']}", "\n\n".join(lines), kind="meeting",
                link=f"{mailer.app_url()}/#/meeting/{meeting_id}", link_text="Open the meeting")
    db.update_meeting(meeting_id, notified_at=db.now())


def send_daily_reminders(today=None):
    """One digest per PM per day. Returns how many emails were sent."""
    today = today or date.today()
    tomorrow = (today + timedelta(days=1)).isoformat()
    digests = []  # collected first; emails go out after the DB transaction is closed
    with db.connect() as c:
        pms = db.rows(c.execute("""
            SELECT DISTINCT e.id, e.full_name, e.email FROM projects p JOIN employees e ON e.id = p.pm_id
            WHERE e.active = 1 AND e.email IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM reminder_log r WHERE r.pm_id = e.id AND r.sent_on = ?)
        """, (today.isoformat(),)))
        for pm in pms:
            tasks = db.rows(c.execute("""
                SELECT t.description, t.due_date, COALESCE(e.full_name, t.assignee) AS who,
                       m.title AS meeting, p.name AS project, t.jira_key
                FROM tasks t JOIN meetings m ON m.id = t.meeting_id JOIN projects p ON p.id = m.project_id
                LEFT JOIN employees e ON e.id = t.assignee_id
                WHERE p.pm_id = ? AND t.status = 'open' AND t.merged_from IS NULL AND t.merged_into IS NULL
                  AND ((t.due_date IS NOT NULL AND t.due_date <= ?)
                       OR t.due_date IS NULL OR COALESCE(e.full_name, t.assignee) IS NULL)
                ORDER BY t.due_date
            """, (pm["id"], tomorrow)))
            c.execute("INSERT OR IGNORE INTO reminder_log (pm_id, sent_on) VALUES (?, ?)", (pm["id"], today.isoformat()))
            if tasks:
                digests.append((pm, tasks))

    fmt = lambda t: (f"• {t['description']} — {t['who'] or 'unassigned'} ({t['project']}, due {t['due_date'] or '-'})"
                     + (f" [{t['jira_key']}]" if t["jira_key"] else ""))
    for pm, tasks in digests:
        dated = [t for t in tasks if t["due_date"] and t["due_date"] <= tomorrow]
        late = [t for t in dated if t["due_date"] < today.isoformat()]
        soon = [t for t in dated if t["due_date"] >= today.isoformat()]
        # Work that came out of a meeting without a person or a deadline is easily forgotten:
        # the PM is reminded every day until both are set.
        no_owner = [t for t in tasks if not t["who"]]
        no_date = [t for t in tasks if t["who"] and not t["due_date"]]
        parts = [f"Hi {pm['full_name'].split()[0]},"]
        if late:
            parts.append(f"LATE ({len(late)})\n" + "\n".join(map(fmt, late)))
        if soon:
            parts.append(f"DUE TODAY OR TOMORROW ({len(soon)})\n" + "\n".join(map(fmt, soon)))
        if no_owner:
            parts.append(f"NOBODY IS RESPONSIBLE ({len(no_owner)})\n" + "\n".join(map(fmt, no_owner)))
        if no_date:
            parts.append(f"NO DEADLINE ({len(no_date)})\n" + "\n".join(map(fmt, no_date)))
        subject = (f"Reminder: {len(late)} late task(s)" if late
                   else f"Reminder: {len(soon)} task(s) due soon" if soon
                   else f"Reminder: {len(no_owner) + len(no_date)} task(s) need a person or a deadline")
        mailer.send(pm["email"], subject, "\n\n".join(parts), kind="reminder",
                    link=f"{mailer.app_url()}/#/tasks", link_text="View tasks")
    return len(digests)


def start_scheduler():
    hour = int(os.environ.get("REMINDER_HOUR", "8"))

    def loop():
        while True:
            try:
                if datetime.now().hour >= hour:
                    send_daily_reminders()
            except Exception:
                traceback.print_exc()
            time.sleep(15 * 60)

    threading.Thread(target=loop, daemon=True, name="reminders").start()
