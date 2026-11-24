"""
"Ask the archive" (RAG in three steps, thesis 4.5):

1. Plan: the language model turns the question into search keywords in Albanian AND English
   (transcripts are Albanian, ticket titles are often English) and the kind of information needed.
2. Sources: transcript passages, summaries, decisions and tasks found with BM25 (SQLite FTS5),
   plus the tasks' CURRENT status and history from the structured memory (synced with Jira/Trello).
   Only a limited number of the most relevant sources is kept (long contexts are used poorly).
3. Answer: only from those sources, citing them by number.

The user searches only the meetings they may see.
"""
import sys
from typing import Literal

from pydantic import BaseModel, Field

from . import db, memory

if str(db.ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(db.ROOT / "scripts"))

import llm  # noqa: E402  (scripts/llm.py)

MAX_SOURCES = 10

SYSTEM_PROMPT = """You answer questions about a team's past meetings using ONLY the
numbered excerpts provided. Answer in the language of the question, in 1-4 sentences.
Excerpts marked [task status] give the CURRENT state of a task (synced with Jira/Trello);
prefer them for questions like "is it done?" or "how many times was the deadline moved?",
and use the meeting excerpts for what was said or decided.
Cite the excerpts you used with their numbers in square brackets, e.g. [1] or [2][3].
If the excerpts do not contain the answer, say so plainly instead of guessing."""

PLAN_PROMPT = """You prepare a search over a team's meeting archive (Albanian transcripts, often in the
Kosovo dialect, with English work terms; ticket titles mostly in English).
Give search keywords in BOTH Albanian and English: names, technical terms, ticket codes, and
synonyms/translations of the key words (e.g. "hyrja në sistem" / "login"). No stop words."""


class SearchPlan(BaseModel):
    keywords: list[str] = Field(description="5-15 single words or short phrases, Albanian and English")
    person: str | None = Field(description="A person the question is about, if any")
    needs: Literal["tasks", "decisions", "summary", "conversation", "any"] = Field(
        description="What kind of information answers the question")


def _format_source(i, s):
    where = f"{s['meeting_title']} ({s['meeting_date']})"
    kind = {"summary": "summary", "decision": "decision", "task": "task", "task_status": "task status"}.get(s["kind"])
    prefix = f"{s['speaker']}: " if s.get("speaker") else (f"[{kind}] " if kind else "")
    return f"[{i}] {where}: {prefix}{s['text']}"


def plan(question):
    """Step 1. Without an AI key the question's own words are used."""
    if not llm.available():
        return SearchPlan(keywords=[question], person=None, needs="any")
    try:
        return llm.structured(PLAN_PROMPT, f"Question: {question}", SearchPlan, max_tokens=1000, effort="low")
    except llm.LLMError:
        return SearchPlan(keywords=[question], person=None, needs="any")


def gather(question, p, meeting_ids=None):
    """Step 2: meeting passages (BM25) + task status from memory, deduplicated, best first."""
    query = " ".join([question, *p.keywords, p.person or ""])
    passages = db.search(query, limit=MAX_SOURCES, meeting_ids=meeting_ids)
    status = memory.task_sources([question, *p.keywords], meeting_ids=meeting_ids,
                                 limit=6 if p.needs in ("tasks", "any") else 3)
    seen, out = set(), []
    # Task status first when the question is about tasks, otherwise interleave after the best passages
    ordered = status + passages if p.needs == "tasks" else passages[:4] + status + passages[4:]
    for s in ordered:
        k = (s["meeting_id"], s["kind"], s["text"][:120])
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out[:MAX_SOURCES]


def ask(question, has_key, meeting_ids=None):
    """meeting_ids: the meetings this user may see (None = all). Raises llm.LLMError."""
    p = plan(question) if has_key else SearchPlan(keywords=[question], person=None, needs="any")
    sources = gather(question, p, meeting_ids)
    for i, s in enumerate(sources, 1):
        s["n"] = i

    if not sources:
        return {"answer": "I found nothing about this in the saved meetings.", "sources": [], "ai": False}
    if not has_key:
        return {"answer": "These are the closest passages found. Add GROQ_API_KEY or ANTHROPIC_API_KEY "
                          "for a full AI answer.", "sources": sources, "ai": False}

    excerpts = "\n".join(_format_source(i, s) for i, s in enumerate(sources, 1))
    answer = llm.text(SYSTEM_PROMPT, f"<excerpts>\n{excerpts}\n</excerpts>\n\nQuestion: {question}", max_tokens=2000)
    return {"answer": answer, "sources": sources, "ai": True, "keywords": p.keywords}
