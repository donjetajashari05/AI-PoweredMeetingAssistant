"""
"Ask the archive" (RAG in three steps; literature review: memory and RAG):

1. Plan: the language model turns the question into search keywords in Albanian AND English
   (transcripts are Albanian, ticket titles are often English) and the kind of information needed.
2. Sources: transcript passages, summaries, decisions and tasks, found by
     bm25      word matching (SQLite FTS5), with the planned Albanian + English keywords;
     semantic  dense retrieval: multilingual sentence embeddings of the question vs every passage,
               so an English question finds an Albanian passage with no shared words;
     hybrid    both lists merged by Reciprocal Rank Fusion (default when embeddings are installed).
   RETRIEVAL_MODE in .env picks one (the evaluation compares all three).
   Plus the tasks' CURRENT status and history from the structured memory (synced with Jira/Trello).
   Only a limited number of the most relevant sources is kept (long contexts are used poorly).
3. Answer: only from those sources, citing them by number.

The user searches only the meetings they may see.
"""
import hashlib
import os
import sys
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from . import db, dedup, memory

if str(db.ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(db.ROOT / "scripts"))

import llm  # noqa: E402  (scripts/llm.py)

MAX_SOURCES = 10
RRF_K = 60            # Reciprocal Rank Fusion constant (Cormack et al.): score = sum of 1 / (k + rank)
SEMANTIC_MIN = 0.35   # passages less similar than this to the question are not used

VECTORS_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunk_vectors (
    hash TEXT PRIMARY KEY,   -- sha1 of model + text, so a passage is embedded only once
    vec BLOB NOT NULL        -- float32, normalized
);
"""

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


def retrieval_mode():
    m = (os.environ.get("RETRIEVAL_MODE") or "").strip().lower()
    if m in ("bm25", "semantic", "hybrid"):
        return m if m == "bm25" or dedup.has_embeddings() else "bm25"
    return "hybrid" if dedup.has_embeddings() else "bm25"


def _vectors(texts):
    """Normalized embeddings, stored in the database so each passage is embedded once."""
    model_name = os.environ.get("EMBEDDING_MODEL") or dedup.DEFAULT_MODEL
    keys = [hashlib.sha1(f"{model_name}\n{t}".encode()).hexdigest() for t in texts]
    found = {}
    with db.connect() as c:
        c.executescript(VECTORS_SCHEMA)
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            found.update(c.execute(f"SELECT hash, vec FROM chunk_vectors WHERE hash IN ({db._in(part)})", part).fetchall())
    missing = [(k, t) for k, t in dict(zip(keys, texts)).items() if k not in found]
    if missing:
        vecs = dedup.embedder().encode([t for _, t in missing], normalize_embeddings=True, batch_size=64)
        rows = [(k, np.asarray(v, dtype=np.float32).tobytes()) for (k, _), v in zip(missing, vecs)]
        with db.connect() as c:
            c.executemany("INSERT OR IGNORE INTO chunk_vectors (hash, vec) VALUES (?, ?)", rows)
        found.update(rows)
    return np.stack([np.frombuffer(found[k], dtype=np.float32) for k in keys])


def semantic_search(question, limit=MAX_SOURCES, meeting_ids=None):
    """Dense retrieval over every passage of the meetings the user may see."""
    if not dedup.has_embeddings():
        return []
    only = "" if meeting_ids is None else f" WHERE m.id IN ({db._in(meeting_ids)})"
    with db.connect() as c:
        rows = db.rows(c.execute(f"""
            SELECT ch.text, ch.kind, ch.start, ch.speaker, ch.meeting_id,
                   m.title AS meeting_title, m.meeting_date
            FROM chunks ch JOIN meetings m ON m.id = ch.meeting_id{only}
        """, tuple(meeting_ids or ())))
    rows = [r for r in rows if (r["text"] or "").strip()]
    if not rows:
        return []
    q = dedup.embedder().encode([question], normalize_embeddings=True)[0]
    sims = _vectors([r["text"] for r in rows]) @ np.asarray(q, dtype=np.float32)
    order = np.argsort(-sims)[:limit]
    return [dict(rows[i], score=round(float(sims[i]), 3)) for i in order if sims[i] >= SEMANTIC_MIN]


def search(question, keyword_query, limit=MAX_SOURCES, meeting_ids=None, mode=None):
    """Passages for a question: bm25 | semantic | hybrid (Reciprocal Rank Fusion of the two)."""
    mode = mode or retrieval_mode()
    if mode == "bm25":
        return db.search(keyword_query, limit=limit, meeting_ids=meeting_ids)
    dense = semantic_search(question, limit=limit * 2, meeting_ids=meeting_ids)
    if mode == "semantic":
        return dense[:limit]
    sparse = db.search(keyword_query, limit=limit * 2, meeting_ids=meeting_ids)
    fused, item = {}, {}
    for ranked in (sparse, dense):
        for rank, s in enumerate(ranked, 1):
            k = (s["meeting_id"], s["kind"], s["text"])
            fused[k] = fused.get(k, 0.0) + 1 / (RRF_K + rank)
            item.setdefault(k, s)
    return [item[k] for k in sorted(fused, key=lambda k: -fused[k])[:limit]]


def gather(question, p, meeting_ids=None, mode=None):
    """Step 2: meeting passages + task status from memory, deduplicated, best first."""
    query = " ".join([question, *p.keywords, p.person or ""])
    passages = search(question, query, limit=MAX_SOURCES, meeting_ids=meeting_ids, mode=mode)
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
