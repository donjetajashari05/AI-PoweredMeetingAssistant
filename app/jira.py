"""
Send confirmed tickets to the project's platform (Jira or Trello).

The platform-specific work is in trackers.py (Adapter pattern); this module only
decides which tickets go out, records the result and tells the assignee.
(The module keeps its old name so existing imports keep working.)
"""
import os

from . import db, memory, trackers

JiraError = trackers.TrackerError  # old name, still used by main.py


def mock():
    return trackers.mock()


def configured():
    return trackers.configured()


def _default_key():
    return (os.environ.get("JIRA_DEFAULT_PROJECT") or "").strip().upper() or ("TEST" if mock() else "")


def _paragraphs(t, link):
    return [
        t.get("details"),
        f"From the meeting “{t['meeting_title']}” ({t['meeting_date']}).",
        f"Quote: “{t['quote']}”" if t.get("quote") else None,
        f"Deadline as said: {t['due_date_text']}" if t.get("due_date_text") else None,
        f"Created by Meeting Assistant: {link}",
    ]


def _send_one(t, sender_id, need_email=True):
    """Create one ticket on its platform. Returns the Issue; raises TrackerError."""
    project = trackers.project_info(t["project_id"])
    platform = (project or {}).get("tracker") or os.environ.get("DEFAULT_TRACKER") or "jira"
    if platform == "jira" and need_email and not t["recipient_email"]:
        raise trackers.TrackerError("Add the email of the person who gets the ticket.")
    tracker = trackers.for_project(project, override_ref=t["jira_project"] if platform == "jira" else None,
                                   employee_id=sender_id)
    link = f"{(os.environ.get('APP_URL') or 'http://127.0.0.1:8000').rstrip('/')}/#/meeting/{t['meeting_id']}"
    assignee = ({"email": t["recipient_email"], "name": t["assignee_name"]}
                if (t["recipient_email"] or t["assignee_name"]) else None)
    issue = tracker.create(t["description"], _paragraphs(t, link), due_date=t["due_date"], assignee=assignee)
    with db.connect() as c:
        c.execute("UPDATE tasks SET jira_key = ?, jira_url = ?, jira_error = NULL, sent_at = ?, sent_by = ?, "
                  "tracker_status = ?, jira_project = COALESCE(jira_project, ?) WHERE id = ?",
                  (issue.key, issue.url, db.now(), sender_id, issue.status,
                   tracker.ref if platform == "jira" else None, t["id"]))
        memory.log(c, "created", task_id=t["id"], key=issue.key, meeting_id=t["meeting_id"], new=issue.key,
                   quote=t.get("quote"), source="user", actor_id=sender_id)
    return issue


def push_tasks(task_ids, sender_id=None):
    """Send the chosen tickets (page "Tickets"). Each ticket is assigned to the person with that email
    (Jira) or the board member with that name (Trello), and the person gets an email about it.
    Returns {"created": [keys], "errors": {task_id: message}}."""
    from . import mailer  # local import: mailer imports db too

    with db.connect() as c:
        tasks = db.rows(c.execute(db.TASK_SELECT + f" WHERE t.id IN ({db._in(task_ids)})", tuple(task_ids)))
    created, errors = [], {}
    for t in tasks:
        if t["jira_key"]:
            errors[t["id"]] = f"Already sent ({t['jira_key']})."
            continue
        if not t["is_ticket"]:
            errors[t["id"]] = "Make a ticket from this task first (page Tasks)."
            continue
        try:
            issue = _send_one(t, sender_id)
        except trackers.TrackerError as e:
            errors[t["id"]] = str(e)
            with db.connect() as c:
                c.execute("UPDATE tasks SET jira_error = ? WHERE id = ?", (str(e), t["id"]))
            continue
        created.append(issue.key)
        if t["recipient_email"]:
            mailer.send(
                t["recipient_email"], f"New ticket for you: {issue.key} {t['description'][:80]}",
                "\n\n".join(x for x in [
                    f"The ticket {issue.key} has been assigned to you.",
                    t["description"], t["details"],
                    f"Due: {t['due_date']}" if t["due_date"] else None,
                    f"From the meeting “{t['meeting_title']}” ({t['meeting_date']}).",
                ] if x),
                kind="ticket", link=issue.url, link_text="Open the ticket")
    return {"created": created, "errors": errors}


def push_meeting(meeting_id, task_ids=None):
    """Create tickets for the meeting's confirmed tickets that don't have one yet.
    Returns {"created": [...keys], "failed": n, "skipped": n}."""
    m = db.meeting_detail(meeting_id)
    created, failed, skipped = [], 0, 0
    for t in m["tasks"]:
        if task_ids is not None and t["id"] not in task_ids:
            continue
        if t["jira_key"] or t["status"] == "done" or not t["approved_at"] or not t["is_ticket"]:
            skipped += 1
            continue
        try:
            created.append(_send_one(t, None, need_email=False).key)
        except trackers.TrackerError as e:
            failed += 1
            with db.connect() as c:
                c.execute("UPDATE tasks SET jira_error = ? WHERE id = ?", (str(e), t["id"]))
    return {"created": created, "failed": failed, "skipped": skipped}
