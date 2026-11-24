"""
Login with username and password (stored in the users table), sessions, and who may see what.

Single-user system: the demo account (admin / admin123) belongs to the administrator.
The owner is the PM and can do everything. Other employees are only meeting participants.
"""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta

from fastapi import HTTPException, Request

from . import db

SESSION_COOKIE = "ma_session"
SESSION_DAYS = 365  # log in once

# Demo account created on first start (thesis project: no email codes, just username + password)
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin123"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
DROP TABLE IF EXISTS login_codes;
"""


def init():
    with db.connect() as c:
        c.executescript(SCHEMA)
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (db.now(),))
        if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            _create_user(c, DEFAULT_USERNAME, DEFAULT_PASSWORD, _admin_employee_id(c))
            print(f"Created the demo login: username '{DEFAULT_USERNAME}', password '{DEFAULT_PASSWORD}'")


def _secret():
    # Session tokens are stored only as keyed hashes
    return (os.environ.get("APP_SECRET") or "change-me-in-env").encode()


def _hash(value):
    return hmac.new(_secret(), value.encode(), hashlib.sha256).hexdigest()


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${digest}"


def check_password(password, stored):
    salt, _, _digest = (stored or "").partition("$")
    return bool(salt) and hmac.compare_digest(hash_password(password, salt), stored)


def admin_emails():
    return {e.strip().lower() for e in (os.environ.get("ADMIN_EMAILS") or "").split(",") if e.strip()}


def allowed_domain(email):
    """If ALLOWED_EMAIL_DOMAINS is set (e.g. company.com), only those domains are accepted."""
    domains = {d.strip().lower().lstrip("@") for d in (os.environ.get("ALLOWED_EMAIL_DOMAINS") or "").split(",") if d.strip()}
    return not domains or email.rsplit("@", 1)[-1] in domains


def is_owner(emp):
    return bool(emp["is_admin"]) or (emp["email"] or "").lower() in admin_emails()


def _employee_by_email(c, email):
    r = c.execute("SELECT * FROM employees WHERE email = ? COLLATE NOCASE AND active = 1", (email,)).fetchone()
    return dict(r) if r else None


def _admin_employee_id(c):
    """The first active administrator, or a new "Admin" employee."""
    r = c.execute("SELECT id FROM employees WHERE is_admin = 1 AND active = 1 ORDER BY id LIMIT 1").fetchone()
    if r:
        return r[0]
    return c.execute("INSERT INTO employees (full_name, email, active, is_admin, created_at) VALUES (?,?,1,1,?)",
                     ("Admin", "admin@example.com", db.now())).lastrowid


def _create_user(c, username, password, employee_id):
    c.execute("INSERT INTO users (username, password_hash, employee_id, created_at) VALUES (?,?,?,?)",
              (username, hash_password(password), employee_id, db.now()))


# ---------- Login ----------

def login(username, password):
    """Return a new session token, or raise 400."""
    username = (username or "").strip()
    with db.connect() as c:
        row = c.execute("""
            SELECT u.password_hash, e.* FROM users u JOIN employees e ON e.id = u.employee_id
            WHERE u.username = ? AND e.active = 1
        """, (username,)).fetchone()
        if not row or not check_password(password or "", row["password_hash"]):
            raise HTTPException(400, "Wrong username or password.")
        token = secrets.token_urlsafe(32)
        c.execute("INSERT INTO sessions (token_hash, employee_id, created_at, expires_at) VALUES (?,?,?,?)",
                  (_hash(token), row["id"], db.now(),
                   (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")))
    return token


def logout(token):
    if token:
        with db.connect() as c:
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))


# ---------- Current user & scope ----------

def user_from_token(token):
    if not token:
        return None
    with db.connect() as c:
        r = c.execute("""
            SELECT e.* FROM sessions s JOIN employees e ON e.id = s.employee_id
            WHERE s.token_hash = ? AND s.expires_at > ? AND e.active = 1
        """, (_hash(token), db.now())).fetchone()
        if not r:
            return None
        user = _build_user(c, dict(r))
        user["is_admin"] = True
    user["role"] = "pm"
    return user


def _build_user(c, user):
    uid = user["id"]
    user["is_admin"] = bool(user["is_admin"]) or (user["email"] or "").lower() in admin_emails()
    user["pm_project_ids"] = [x[0] for x in c.execute("SELECT id FROM projects WHERE pm_id = ?", (uid,))]
    user["project_ids"] = [x[0] for x in c.execute("SELECT project_id FROM project_members WHERE employee_id = ?", (uid,))]
    user["team_ids"] = [x[0] for x in c.execute("SELECT team_id FROM team_members WHERE employee_id = ?", (uid,))]
    user["is_pm"] = bool(user["pm_project_ids"])
    user["role"] = "admin" if user["is_admin"] else "pm" if user["is_pm"] else "employee"
    return user


def login_disabled():
    """LOGIN_DISABLED=1 skips the login entirely (local testing only)."""
    return (os.environ.get("LOGIN_DISABLED") or "0").strip().lower() in ("1", "true", "yes")


def _local_admin():
    with db.connect() as c:
        r = c.execute("SELECT id FROM employees WHERE is_admin = 1 AND active = 1 ORDER BY id LIMIT 1").fetchone()
        if r:
            emp_id = r[0]
        else:
            email = next(iter(sorted(admin_emails())), "admin@local")
            emp = _employee_by_email(c, email)
            if emp:
                emp_id = emp["id"]
                c.execute("UPDATE employees SET is_admin = 1 WHERE id = ?", (emp_id,))
            else:
                emp_id = c.execute("INSERT INTO employees (full_name, email, active, is_admin, created_at) VALUES (?,?,1,1,?)",
                                   ("Admin", email, db.now())).lastrowid
        user = _build_user(c, dict(c.execute("SELECT * FROM employees WHERE id = ?", (emp_id,)).fetchone()))
    # Single-user system: the owner is the PM and can do everything; everyone else is only a meeting participant
    user["is_admin"] = True
    user["role"] = "pm"
    return user


def current_user(request: Request):
    """FastAPI dependency: the logged-in user, or 401."""
    if login_disabled():
        return _local_admin()
    user = user_from_token(request.cookies.get(SESSION_COOKIE))
    if not user:
        raise HTTPException(401, "Please sign in.")
    return user


def require_admin(user):
    if not user["is_admin"]:
        raise HTTPException(403, "Only an administrator can do this.")


def visible_meeting_ids(user):
    """None = all meetings (admin). Otherwise the ids this user may open."""
    if user["is_admin"]:
        return None
    projects = set(user["project_ids"]) | set(user["pm_project_ids"])
    with db.connect() as c:
        rows = c.execute(f"""
            SELECT id FROM meetings
            WHERE created_by = ?
               OR project_id IN ({','.join('?' * len(projects)) or 'NULL'})
               OR (project_id IS NULL AND team_id IN ({','.join('?' * len(user['team_ids'])) or 'NULL'}))
        """, (user["id"], *projects, *user["team_ids"])).fetchall()
    return {r[0] for r in rows}


def can_manage_meeting(user, meeting):
    """Admin, the PM of the meeting's project, or whoever added it."""
    return (user["is_admin"] or meeting.get("created_by") == user["id"]
            or (meeting.get("project_id") in user["pm_project_ids"]))


def check_meeting(user, meeting_id, manage=False):
    m = db.get_meeting(meeting_id)
    visible = visible_meeting_ids(user)
    if not m or (visible is not None and meeting_id not in visible):
        raise HTTPException(404, "Meeting not found.")
    if manage and not can_manage_meeting(user, m):
        raise HTTPException(403, "Only the project’s PM or an administrator can do this.")
    return m


def task_scope(user):
    """(meeting_ids, assignee_id) filter for task lists. Admin: all.
    PM: every task of their projects' meetings + own. Employee: own tasks."""
    if user["is_admin"]:
        return None, None
    if user["is_pm"]:
        with db.connect() as c:
            ids = {r[0] for r in c.execute(
                f"SELECT id FROM meetings WHERE project_id IN ({','.join('?' * len(user['pm_project_ids']))})",
                user["pm_project_ids"])}
        return ids, user["id"]  # tasks in those meetings OR assigned to me
    return set(), user["id"]


def can_edit_task(user, task, reassign=False):
    m = db.get_meeting(task["meeting_id"])
    if user["is_admin"] or (m and can_manage_meeting(user, m)):
        return True
    return not reassign and task.get("assignee_id") == user["id"]
