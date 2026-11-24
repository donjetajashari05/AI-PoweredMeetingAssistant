"""
Jira and Trello behind ONE interface (Adapter pattern), so the rest of the app
doesn't care which platform a team uses.

Every adapter implements:
    list_issues()                      existing tickets of the project/board (open + recently closed)
    create(title, paragraphs, due_date, assignee)   -> Issue
    update(key, due_date=, assignee=, comment=)     change deadline / person, add a comment
    reopen(key, comment=)              Jira: workflow transition back to an open status;
                                       Trello: move the card from the "Done" list to the open list
    status(key)                        -> (status name, is_done)

Credentials: each PM connects their own Jira or Trello account (page "Connections");
the token is stored encrypted (Fernet, key derived from APP_SECRET). Without a PM
account the .env values are used (JIRA_* / TRELLO_KEY + TRELLO_TOKEN).

JIRA_MOCK=1 (or TRACKER_MOCK=1): no server is called; issues live in data/tracker_mock.json.
Put hand-made issues there to simulate tickets the team created directly on the platform.
"""
import base64
import hashlib
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

from . import db

PLATFORMS = ("jira", "trello")
MOCK_FILE = db.ROOT / "data" / "tracker_mock.json"
_mock_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracker_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,            -- jira | trello
    base_url TEXT,                     -- Jira only, e.g. https://company.atlassian.net
    username TEXT,                     -- Jira: account email; Trello: API key
    token_enc TEXT NOT NULL,           -- encrypted API token
    created_at TEXT NOT NULL,
    UNIQUE (employee_id, platform)
);
"""


class TrackerError(Exception):
    pass


@dataclass
class Issue:
    key: str
    title: str
    status: str = ""
    done: bool = False
    assignee: str | None = None
    due_date: str | None = None
    url: str | None = None
    platform: str = "jira"

    def dict(self):
        return asdict(self)


def init():
    with db.connect() as c:
        c.executescript(SCHEMA)


def mock():
    flag = (os.environ.get("TRACKER_MOCK") or os.environ.get("JIRA_MOCK") or "").strip().lower()
    return flag in ("1", "true", "yes")


# ---------- Token encryption ----------

def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise TrackerError("Install the 'cryptography' package (pip install -r requirements.txt) to store tokens.")
    secret = (os.environ.get("APP_SECRET") or "").encode()
    if not secret:
        raise TrackerError("Set APP_SECRET in .env before connecting an account: it encrypts the tokens.")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"tracker-token:" + secret).digest()))


def encrypt(token):
    return _fernet().encrypt(token.encode()).decode()


def decrypt(token_enc):
    try:
        return _fernet().decrypt(token_enc.encode()).decode()
    except TrackerError:
        raise
    except Exception:
        raise TrackerError("The saved token can't be read (was APP_SECRET changed?). Connect the account again.")


def save_account(employee_id, platform, username, token, base_url=None):
    if platform not in PLATFORMS:
        raise TrackerError("Unknown platform.")
    if platform == "jira":
        base_url = (base_url or "").strip().rstrip("/")
        if not re.match(r"^https://[\w.-]+$", base_url):
            raise TrackerError("The Jira address must look like https://company.atlassian.net")
    username, token = (username or "").strip(), (token or "").strip()
    if not username or not token:
        raise TrackerError("Fill in both fields.")
    with db.connect() as c:
        c.execute("""INSERT INTO tracker_accounts (employee_id, platform, base_url, username, token_enc, created_at)
                     VALUES (?,?,?,?,?,?)
                     ON CONFLICT(employee_id, platform) DO UPDATE SET
                       base_url = excluded.base_url, username = excluded.username,
                       token_enc = excluded.token_enc, created_at = excluded.created_at""",
                  (employee_id, platform, base_url if platform == "jira" else None, username, encrypt(token), db.now()))


def delete_account(employee_id, platform):
    with db.connect() as c:
        c.execute("DELETE FROM tracker_accounts WHERE employee_id = ? AND platform = ?", (employee_id, platform))


def accounts_of(employee_id):
    """What the PM has connected (never the token)."""
    with db.connect() as c:
        rows = db.rows(c.execute("SELECT platform, base_url, username, created_at FROM tracker_accounts "
                                 "WHERE employee_id = ?", (employee_id,)))
    for r in rows:
        if r["platform"] == "trello":  # the API key is not secret, but show only its start
            r["username"] = r["username"][:6] + "…"
    return rows


def _account(employee_id, platform):
    if not employee_id:
        return None
    with db.connect() as c:
        r = c.execute("SELECT * FROM tracker_accounts WHERE employee_id = ? AND platform = ?",
                      (employee_id, platform)).fetchone()
    return dict(r) if r else None


# ---------- HTTP ----------

def _http(method, url, headers, body=None, form=None):
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers = {**headers, "Content-Type": "application/json"}
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, method=method, data=data,
                                 headers={"Accept": "application/json", "User-Agent": "meeting-memory", **headers})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read())
            if isinstance(detail, dict):
                msgs = detail.get("errorMessages", []) + [f"{k}: {v}" for k, v in (detail.get("errors") or {}).items()]
                msg = "; ".join(msgs) or detail.get("message") or str(detail)
            else:
                msg = str(detail)
        except Exception:
            msg = e.reason
        raise TrackerError(f"{e.code}: {msg}"[:400])
    except urllib.error.URLError as e:
        raise TrackerError(f"The platform could not be reached: {e.reason}")


def _adf(paragraphs):
    """Jira's document format (ADF) for plain paragraphs."""
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paragraphs if p]}


# ---------- Adapters ----------

class Tracker:
    platform = ""

    def __init__(self, ref):
        self.ref = ref  # Jira project key or Trello board id

    def list_issues(self) -> list[Issue]:
        raise NotImplementedError

    def create(self, title, paragraphs, due_date=None, assignee=None) -> Issue:
        raise NotImplementedError

    def update(self, key, due_date=None, assignee=None, comment=None):
        raise NotImplementedError

    def reopen(self, key, comment=None):
        raise NotImplementedError

    def status(self, key):
        raise NotImplementedError


class JiraTracker(Tracker):
    """assignee = {"email": ..., "name": ...}; the Jira user is found by email."""
    platform = "jira"

    def __init__(self, ref, base_url, email, token):
        super().__init__(ref)
        self.base = base_url.rstrip("/")
        self._auth = {"Authorization": "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()}
        self._users = {}

    def _req(self, method, path, body=None):
        try:
            return _http(method, self.base + path, self._auth, body=body)
        except TrackerError as e:
            raise TrackerError(f"Jira {e}")

    def _account_id(self, assignee):
        email = (assignee or {}).get("email")
        if not email:
            return None
        if email not in self._users:
            users = self._req("GET", "/rest/api/3/user/search?" + urllib.parse.urlencode({"query": email})) or []
            exact = [u for u in users if (u.get("emailAddress") or "").lower() == email.lower()]
            pick = exact[0] if exact else (users[0] if len(users) == 1 else None)
            self._users[email] = pick["accountId"] if pick else None
        return self._users[email]

    def _issue(self, raw):
        f = raw.get("fields") or {}
        st = f.get("status") or {}
        return Issue(key=raw["key"], title=f.get("summary") or "", status=st.get("name") or "",
                     done=(st.get("statusCategory") or {}).get("key") == "done",
                     assignee=(f.get("assignee") or {}).get("displayName"), due_date=f.get("duedate"),
                     url=f"{self.base}/browse/{raw['key']}", platform="jira")

    def list_issues(self):
        jql = f'project = "{self.ref}" AND (statusCategory != Done OR updated >= -90d) ORDER BY updated DESC'
        out, token = [], None
        while len(out) < 500:
            body = {"jql": jql, "fields": ["summary", "status", "assignee", "duedate"], "maxResults": 100}
            if token:
                body["nextPageToken"] = token
            res = self._req("POST", "/rest/api/3/search/jql", body) or {}
            out += [self._issue(i) for i in res.get("issues", [])]
            token = res.get("nextPageToken")
            if not token or res.get("isLast", True):
                break
        return out

    def create(self, title, paragraphs, due_date=None, assignee=None):
        fields = {"project": {"key": self.ref}, "issuetype": {"name": os.environ.get("JIRA_ISSUE_TYPE", "Task")},
                  "summary": title[:250], "description": _adf(paragraphs), "labels": ["meeting-memory"]}
        if due_date:
            fields["duedate"] = due_date
        account = self._account_id(assignee)
        if assignee and assignee.get("email") and not account:
            raise TrackerError(f"No Jira user was found with the email {assignee['email']}.")
        if account:
            fields["assignee"] = {"accountId": account}
        key = self._req("POST", "/rest/api/3/issue", {"fields": fields})["key"]
        return Issue(key=key, title=title, status="To Do", url=f"{self.base}/browse/{key}", due_date=due_date,
                     platform="jira")

    def update(self, key, due_date=None, assignee=None, comment=None):
        fields = {}
        if due_date:
            fields["duedate"] = due_date
        if assignee:
            account = self._account_id(assignee)
            if not account:
                raise TrackerError(f"No Jira user was found with the email {assignee.get('email')}.")
            fields["assignee"] = {"accountId": account}
        if fields:
            self._req("PUT", f"/rest/api/3/issue/{key}", {"fields": fields})
        if comment:
            self._req("POST", f"/rest/api/3/issue/{key}/comment", {"body": _adf(comment.split("\n\n"))})

    def reopen(self, key, comment=None):
        transitions = (self._req("GET", f"/rest/api/3/issue/{key}/transitions") or {}).get("transitions", [])

        def rank(t):
            to = t.get("to") or {}
            cat = (to.get("statusCategory") or {}).get("key")
            name = f"{t.get('name', '')} {to.get('name', '')}".lower()
            return (cat == "done", not re.search(r"reopen|to ?do|open|backlog|hapur", name), cat != "new")

        options = [t for t in transitions if ((t.get("to") or {}).get("statusCategory") or {}).get("key") != "done"]
        if not options:
            raise TrackerError(f"Jira: {key} has no transition back to an open status in its workflow.")
        self._req("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": sorted(options, key=rank)[0]["id"]}})
        if comment:
            self.update(key, comment=comment)

    def status(self, key):
        raw = self._req("GET", f"/rest/api/3/issue/{key}?fields=status")
        i = self._issue({**raw, "fields": {**raw.get("fields", {}), "summary": ""}})
        return i.status, i.done


DONE_LIST = re.compile(r"done|complete|closed|kryer|p[eë]rfundu|mbyll", re.I)
OPEN_LIST = re.compile(r"to ?do|backlog|open|hapur|p[eë]r t[eë] b[eë]r", re.I)


class TrelloTracker(Tracker):
    """A task is a card. Done = the card is in the board's "Done" list (or marked complete).
    assignee = {"name": ...}: Trello can't look people up by email, so board members are matched by name."""
    platform = "trello"
    API = "https://api.trello.com/1"

    def __init__(self, ref, api_key, token):
        super().__init__(ref)
        # Header auth keeps the token out of URLs (and out of server logs)
        self._auth = {"Authorization": f'OAuth oauth_consumer_key="{api_key}", oauth_token="{token}"'}
        self._lists = None
        self._members = None

    def _req(self, method, path, body=None, form=None):
        try:
            return _http(method, self.API + path, self._auth, body=body, form=form)
        except TrackerError as e:
            raise TrackerError(f"Trello {e}")

    def lists(self):
        if self._lists is None:
            lists = self._req("GET", f"/boards/{self.ref}/lists?filter=open&fields=name") or []
            if not lists:
                raise TrackerError("Trello: the board has no lists.")
            done = next((l for l in reversed(lists) if DONE_LIST.search(l["name"])), None)
            open_ = next((l for l in lists if OPEN_LIST.search(l["name"]) and l is not done),
                         next((l for l in lists if l is not done), lists[0]))
            self._lists = {"all": {l["id"]: l["name"] for l in lists}, "done": done and done["id"], "open": open_["id"]}
        return self._lists

    def _member_id(self, assignee):
        name = (assignee or {}).get("name")
        if not name:
            return None
        if self._members is None:
            self._members = self._req("GET", f"/boards/{self.ref}/members?fields=fullName,username") or []
        norm = lambda s: re.sub(r"\s+", " ", (s or "").strip().lower())
        first = norm(name).split(" ")[0]
        exact = [m for m in self._members if norm(m.get("fullName")) == norm(name)]
        loose = [m for m in self._members if norm(m.get("fullName")).split(" ")[0] == first]
        pick = exact or (loose if len(loose) == 1 else [])
        return pick[0]["id"] if pick else None

    def _issue(self, card):
        lists = self.lists()
        done = bool(card.get("closed") or card.get("dueComplete") or (lists["done"] and card.get("idList") == lists["done"]))
        return Issue(key=card.get("shortLink") or card["id"], title=card.get("name") or "",
                     status=lists["all"].get(card.get("idList"), "Archived" if card.get("closed") else ""),
                     done=done, due_date=(card.get("due") or "")[:10] or None, url=card.get("shortUrl"),
                     platform="trello")

    def list_issues(self):
        cards = self._req("GET", f"/boards/{self.ref}/cards/all?fields=name,idList,due,dueComplete,closed,shortLink,shortUrl,dateLastActivity") or []
        return [self._issue(c) for c in cards[:500]]

    def create(self, title, paragraphs, due_date=None, assignee=None):
        form = {"idList": self.lists()["open"], "name": title[:16384], "desc": "\n\n".join(p for p in paragraphs if p)}
        if due_date:
            form["due"] = f"{due_date}T12:00:00.000Z"
        member = self._member_id(assignee)
        if member:
            form["idMembers"] = member
        card = self._req("POST", "/cards", form=form)
        return self._issue(card)

    def update(self, key, due_date=None, assignee=None, comment=None):
        form = {}
        if due_date:
            form["due"] = f"{due_date}T12:00:00.000Z"
        if assignee:
            member = self._member_id(assignee)
            if not member:
                raise TrackerError(f"Trello: {assignee.get('name')} is not a member of the board.")
            form["idMembers"] = member
        if form:
            self._req("PUT", f"/cards/{key}", form=form)
        if comment:
            self._req("POST", f"/cards/{key}/actions/comments", form={"text": comment})

    def reopen(self, key, comment=None):
        self._req("PUT", f"/cards/{key}", form={"idList": self.lists()["open"], "closed": "false", "dueComplete": "false"})
        if comment:
            self.update(key, comment=comment)

    def status(self, key):
        i = self._issue(self._req("GET", f"/cards/{key}?fields=idList,closed,dueComplete,name,shortLink,shortUrl,due"))
        return i.status, i.done


class MockTracker(Tracker):
    """Offline stand-in for tests: issues in data/tracker_mock.json, grouped by project/board."""

    def __init__(self, ref, platform="jira"):
        super().__init__(ref)
        self.platform = platform

    def _load(self):
        if MOCK_FILE.is_file():
            with open(MOCK_FILE, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save(self, data):
        MOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(MOCK_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _find(self, data, key):
        for i in data.get(self.ref, []):
            if i["key"] == key:
                return i
        raise TrackerError(f"Mock: {key} not found.")

    def list_issues(self):
        with _mock_lock:
            return [Issue(**{k: v for k, v in i.items() if k in Issue.__dataclass_fields__})
                    for i in self._load().get(self.ref, [])]

    def create(self, title, paragraphs, due_date=None, assignee=None):
        with _mock_lock:
            data = self._load()
            issues = data.setdefault(self.ref, [])
            n = max([int(i["key"].rsplit("-", 1)[-1]) for i in issues if i["key"].rsplit("-", 1)[-1].isdigit()] + [0]) + 1
            issue = Issue(key=f"{self.ref}-{n}", title=title, status="To Do", due_date=due_date,
                          assignee=(assignee or {}).get("name") or (assignee or {}).get("email"),
                          url=f"mock://{self.ref}-{n}", platform=self.platform)
            issues.append({**issue.dict(), "description": paragraphs, "comments": [], "created_at": db.now()})
            self._save(data)
            return issue

    def update(self, key, due_date=None, assignee=None, comment=None):
        with _mock_lock:
            data = self._load()
            i = self._find(data, key)
            if due_date:
                i["due_date"] = due_date
            if assignee:
                i["assignee"] = assignee.get("name") or assignee.get("email")
            if comment:
                i.setdefault("comments", []).append(comment)
            self._save(data)

    def reopen(self, key, comment=None):
        with _mock_lock:
            data = self._load()
            i = self._find(data, key)
            i.update(status="To Do", done=False)
            if comment:
                i.setdefault("comments", []).append(comment)
            self._save(data)

    def status(self, key):
        with _mock_lock:
            i = self._find(self._load(), key)
            return i.get("status", ""), bool(i.get("done"))


# ---------- Which tracker a project uses ----------

def project_info(project_id):
    if not project_id:
        return None
    with db.connect() as c:
        r = c.execute("SELECT id, name, pm_id, tracker, jira_key, trello_board FROM projects WHERE id = ?",
                      (project_id,)).fetchone()
    return dict(r) if r else None


def _platform_of(project):
    p = ((project or {}).get("tracker") or os.environ.get("DEFAULT_TRACKER") or "jira").lower()
    return p if p in PLATFORMS else "jira"


def ref_of(project, override=None):
    """Jira project key or Trello board id for this project (override = the ticket's own key)."""
    platform = _platform_of(project)
    if platform == "trello":
        return override or (project or {}).get("trello_board") or (os.environ.get("TRELLO_DEFAULT_BOARD") or "").strip()
    return (override or (project or {}).get("jira_key") or (os.environ.get("JIRA_DEFAULT_PROJECT") or "").strip().upper()
            or ("TEST" if mock() else ""))


def for_project(project, override_ref=None, employee_id=None) -> Tracker:
    """The adapter for a project row (see project_info). Raises TrackerError if it can't be used."""
    platform = _platform_of(project)
    ref = ref_of(project, override_ref)
    if not ref:
        raise TrackerError("Set the Jira project key or Trello board on the project (page Team)."
                           if platform == "jira" else "Set the Trello board id on the project (page Team).")
    if mock():
        return MockTracker(ref, platform)
    acc = _account((project or {}).get("pm_id"), platform) or _account(employee_id, platform)
    if platform == "jira":
        if acc:
            return JiraTracker(ref, acc["base_url"], acc["username"], decrypt(acc["token_enc"]))
        if all(os.environ.get(k) for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")):
            return JiraTracker(ref, os.environ["JIRA_BASE_URL"], os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
        raise TrackerError("Jira is not connected. Connect your Jira account (page Connections) or fill JIRA_* in .env.")
    if acc:
        return TrelloTracker(ref, acc["username"], decrypt(acc["token_enc"]))
    if os.environ.get("TRELLO_KEY") and os.environ.get("TRELLO_TOKEN"):
        return TrelloTracker(ref, os.environ["TRELLO_KEY"], os.environ["TRELLO_TOKEN"])
    raise TrackerError("Trello is not connected. Connect your Trello account (page Connections) or fill TRELLO_* in .env.")


def configured(platform=None):
    """Is any account available (for the status panel)?"""
    if mock():
        return True
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM tracker_accounts" + (" WHERE platform = ?" if platform else ""),
                      (platform,) if platform else ()).fetchone()[0]
    env = {"jira": all(os.environ.get(k) for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")),
           "trello": bool(os.environ.get("TRELLO_KEY") and os.environ.get("TRELLO_TOKEN"))}
    return bool(n) or (env[platform] if platform else any(env.values()))
