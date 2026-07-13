"""vector_store.py — minimal semantic-search backend on sqlite-vec.

Responsibilities:
- Lazy-load embedding model (sentence-transformers/all-MiniLM-L6-v2, 384 dim).
- Lazy-init sqlite-vec extension on a given connection.
- Chunk text by sentence-windows, embed, store.
- Query: embed a question, return top-k (slug, idx, text, distance).

Storage layout: virtual table `chunks` for vectors + ordinary table
`chunk_meta` for {slug, idx, text} keyed by rowid.

Ponytail constraints honored:
- No new abstractions (one module, four functions).
- One self-check at the bottom (`if __name__ == "__main__"`).
- Failure modes fall back to bundle-and-ask, not crash.
"""

from __future__ import annotations
import re
import sqlite3
from pathlib import Path
from typing import Optional

# ponytail: constants matched to the corpus spec. Chunk size ~120 words
# (≈ 480 tokens) — fits comfortably in a Gemini prompt with k=10 chunks.
CHUNK_WORDS = 120
CHUNK_OVERLAP_WORDS = 20
EMBED_DIM = 384
EMBED_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

_MODEL = None


def _model():
    """Lazy-load the embedding model on first call."""
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer(EMBED_MODEL_NAME)
    return _MODEL


def ensure_vec(conn: sqlite3.Connection) -> None:
    """Create the vec table + metadata table if missing. Idempotent."""
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING vec0("
        f"embedding float[{EMBED_DIM}])"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chunk_meta ("
        "  rowid INTEGER PRIMARY KEY, "
        "  slug  TEXT NOT NULL, "
        "  idx   INTEGER NOT NULL, "
        "  text  TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS chunk_meta_slug ON chunk_meta(slug)"
    )
    conn.commit()


def _pack(vec) -> bytes:
    """numpy float32 → bytes (sqlite-vec's expected wire format)."""
    import numpy as np
    return np.asarray(vec, dtype=np.float32).tobytes()


def _split_words(text: str) -> list[str]:
    return [w for w in re.split(r"\s+", text.strip()) if w]


def chunk_text(text: str, chunk_words: int = CHUNK_WORDS,
               overlap: int = CHUNK_OVERLAP_WORDS) -> list[str]:
    """Word-window chunker. Overlap preserves continuity at chunk boundaries."""
    words = _split_words(text)
    if not words:
        return []
    if len(words) <= chunk_words:
        return [" ".join(words)]
    chunks, start = [], 0
    while start < len(words):
        end = min(start + chunk_words, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start = max(end - overlap, start + 1)
    return chunks


def embed(texts: list[str]) -> list:
    """Encode N strings → N 384-dim vectors. Lazy-loads the model."""
    import numpy as np
    arr = _model().encode(texts, convert_to_numpy=True,
                          show_progress_bar=False, normalize_embeddings=True)
    return [np.asarray(v, dtype=np.float32) for v in arr]


def upsert_chunks(conn: sqlite3.Connection, slug: str,
                  transcript: str) -> int:
    """Delete any prior chunks for slug, then store fresh ones. Returns count."""
    ensure_vec(conn)
    # ponytail: re-embed is idempotent — old rows removed by rowid linkage.
    old = conn.execute(
        "SELECT rowid FROM chunk_meta WHERE slug = ?", (slug,)
    ).fetchall()
    for (rid,) in old:
        conn.execute("DELETE FROM chunks WHERE rowid = ?", (rid,))
    conn.execute("DELETE FROM chunk_meta WHERE slug = ?", (slug,))
    conn.commit()

    chunks = chunk_text(transcript)
    if not chunks:
        return 0
    vectors = embed(chunks)
    inserted = 0
    for idx, (text, vec) in enumerate(zip(chunks, vectors)):
        conn.execute("INSERT INTO chunks(embedding) VALUES (?)", [_pack(vec)])
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO chunk_meta(rowid, slug, idx, text) VALUES (?, ?, ?, ?)",
            (rid, slug, idx, text),
        )
        inserted += 1
    conn.commit()
    return inserted


# ponytail: lift the "which text do we embed?" decision into one helper.
# Multimodal-mode analyses have no transcript sidecar; the search corpus
# shouldn't be missing 5 of 8 files because they had no captions to fetch.
def body_text_for_indexing(md_path) -> str:
    """Return the best-available text for vector indexing.

    Order:
      1. Transcript sidecar (authoritative, was-is said).
      2. Markdown body "## 1. Summary" + "## 2. Key Takeaways" sections.

    Returns "" if neither yields usable text (~50 chars heuristic)."""
    import re as _re
    p = Path(md_path)
    tpath = p.with_suffix("")  # <slug>.md → <slug>.transcript.txt
    tpath = tpath.parent / (p.stem + ".transcript.txt")
    if tpath.exists():
        return tpath.read_text(encoding="utf-8", errors="replace")
    text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
    if not text:
        return ""
    parts = []
    m1 = _re.search(r"##\s*1\.\s*Summary\s*\n(.*?)(?=\n##\s|\Z)", text, _re.S)
    if m1: parts.append(m1.group(1).strip())
    m2 = _re.search(r"##\s*2\.\s*Key Takeaways\s*\n(.*?)(?=\n##\s|\Z)",
                    text, _re.S)
    if m2: parts.append(m2.group(1).strip())
    return "\n\n".join(parts)


def search(conn: sqlite3.Connection, query: str, k: int = 10) -> list[dict]:
    """Top-k nearest chunks by cosine (sqlite-vec normalizes inputs; we normalize
    embeddings at write-time so the default L2 distance becomes equivalent to
    cosine ranking for unit vectors). Returns [{slug, idx, text, distance}]."""
    ensure_vec(conn)
    if not query.strip():
        return []
    q_vec = embed([query])[0]
    rows = conn.execute(
        "SELECT rowid, distance FROM chunks WHERE embedding MATCH ? "
        "ORDER BY distance LIMIT ?",
        [_pack(q_vec), k],
    ).fetchall()
    out = []
    for rid, dist in rows:
        meta = conn.execute(
            "SELECT slug, idx, text FROM chunk_meta WHERE rowid = ?", (rid,)
        ).fetchone()
        if meta:
            out.append({"slug": meta[0], "idx": meta[1],
                        "text": meta[2], "distance": float(dist)})
    return out


# ponytail: chat persistence lives in the same SQLite file. No new module.
# Schema: chat_messages(session_id, role, content, ts). role ∈ {user, model}.
CHAT_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT    NOT NULL,
        role       TEXT    NOT NULL,
        content    TEXT    NOT NULL,
        ts         REAL    NOT NULL
    )
"""

# ponytail: tag assignments for classification filtering.
# One row per (slug, tag). Multiple tags per slug allowed.
TAG_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS tag_assignments (
        slug TEXT    NOT NULL,
        tag  TEXT    NOT NULL,
        ts   REAL    NOT NULL,
        PRIMARY KEY (slug, tag)
    )
"""

# ponytail: fixed vocabulary for D3 classification. Add new tags as needed;
# the classifier prompt is told to pick from this set or emit "other".
TAG_VOCAB = (
    "ai-tooling",
    "founder-psychology",
    "investing",
    "personal-development",
    "religion-or-faith",
    "history-or-politics",
    "music-or-performance",
    "lifestyle-or-cooking",
    "other",
)


def init_chat(conn: sqlite3.Connection) -> None:
    conn.execute(CHAT_TABLE_SQL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS chat_messages_session "
        "ON chat_messages(session_id, id)"
    )
    conn.commit()


def save_message(conn: sqlite3.Connection, session_id: str,
                 role: str, content: str) -> None:
    import time as _t
    init_chat(conn)
    conn.execute(
        "INSERT INTO chat_messages(session_id, role, content, ts) "
        "VALUES (?, ?, ?, ?)",
        (session_id, role, content, _t.time()),
    )
    conn.commit()


def load_messages(conn: sqlite3.Connection, session_id: str,
                  limit: int = 8) -> list[dict]:
    """Return the most recent `limit` messages for a session, oldest first."""
    init_chat(conn)
    rows = conn.execute(
        "SELECT role, content, ts FROM chat_messages "
        "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [{"role": r[0], "content": r[1], "ts": r[2]} for r in reversed(rows)]


def clear_session(conn: sqlite3.Connection, session_id: str) -> int:
    init_chat(conn)
    cur = conn.execute(
        "DELETE FROM chat_messages WHERE session_id = ?", (session_id,)
    )
    conn.commit()
    return cur.rowcount


def list_sessions(conn: sqlite3.Connection) -> list[dict]:
    init_chat(conn)
    rows = conn.execute(
        "SELECT session_id, COUNT(*), MIN(ts), MAX(ts) "
        "FROM chat_messages GROUP BY session_id ORDER BY MAX(ts) DESC"
    ).fetchall()
    return [{"session_id": r[0], "count": r[1], "first_ts": r[2], "last_ts": r[3]}
            for r in rows]


# ponytail: tag CRUD. Keeps vocabulary concerns in one place. No new module.

def init_tags(conn: sqlite3.Connection) -> None:
    conn.execute(TAG_TABLE_SQL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS tag_assignments_tag "
        "ON tag_assignments(tag)"
    )
    conn.commit()


def set_tags(conn: sqlite3.Connection, slug: str, tags: list[str]) -> None:
    """Replace the tag set for a slug. Idempotent."""
    import time as _t
    init_tags(conn)
    conn.execute("DELETE FROM tag_assignments WHERE slug = ?", (slug,))
    now = _t.time()
    seen = set()
    for t in tags:
        t = (t or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        conn.execute(
            "INSERT INTO tag_assignments(slug, tag, ts) VALUES (?, ?, ?)",
            (slug, t, now),
        )
    conn.commit()


def get_tags(conn: sqlite3.Connection, slug: str) -> list[str]:
    init_tags(conn)
    rows = conn.execute(
        "SELECT tag FROM tag_assignments WHERE slug = ? ORDER BY tag",
        (slug,),
    ).fetchall()
    return [r[0] for r in rows]


def get_slugs_by_tag(conn: sqlite3.Connection, tag: str) -> set[str]:
    """Used by ask.py --tag filter."""
    init_tags(conn)
    rows = conn.execute(
        "SELECT slug FROM tag_assignments WHERE tag = ?", (tag,)
    ).fetchall()
    return {r[0] for r in rows}

# ponytail: self-check extension. Verifies init + save + load + clear roundtrip.
if __name__ == "__main__":
    import tempfile, os, sys
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)

    # --- existing test ---
    sample = ("This is a test transcript about vector stores. " * 50 +
              "It talks about cosine similarity and sqlite-vec. " * 30)
    n = upsert_chunks(conn, "testslug", sample)
    print(f"inserted {n} chunks")

    hits = search(conn, "tell me about cosine similarity", k=3)
    for h in hits:
        print(f"  hit slug={h['slug']} idx={h['idx']} dist={h['distance']:.3f} "
              f"text={h['text'][:80]!r}")
    assert hits, "expected at least one hit"
    assert any("cosine" in h["text"] for h in hits), \
        "expected cosine chunk in top-k"
    print("OK  vector_store self-check passed")

    # --- chat-store test ---
    init_chat(conn)
    save_message(conn, "s1", "user", "hi")
    save_message(conn, "s1", "model", "hello")
    save_message(conn, "s1", "user", "what's up")
    msgs = load_messages(conn, "s1")
    assert len(msgs) == 3, f"expected 3 msgs, got {len(msgs)}"
    assert msgs[0]["role"] == "user" and msgs[2]["content"] == "what's up", \
        f"order/content wrong: {msgs}"
    cleared = clear_session(conn, "s1")
    assert cleared == 3, f"expected 3 cleared, got {cleared}"
    msgs = load_messages(conn, "s1")
    assert msgs == [], f"expected empty after clear, got {msgs}"
    print("OK  chat_store self-check passed")

    # --- tag-store test ---
    init_tags(conn)
    set_tags(conn, "vid1", ["ai-tooling", "founder-psychology"])
    set_tags(conn, "vid2", ["ai-tooling"])
    set_tags(conn, "vid3", ["other"])
    set_tags(conn, "vid1", ["ai-tooling", "investing"])  # replace, not append
    assert get_tags(conn, "vid1") == ["ai-tooling", "investing"], \
        f"set_tags should replace: got {get_tags(conn, 'vid1')}"
    assert get_slugs_by_tag(conn, "ai-tooling") == {"vid1", "vid2"}, \
        f"expected {{vid1, vid2}}, got {get_slugs_by_tag(conn, 'ai-tooling')}"
    assert get_slugs_by_tag(conn, "investing") == {"vid1"}, \
        f"expected {{vid1}}, got {get_slugs_by_tag(conn, 'investing')}"
    print("OK  tag_store self-check passed")

    conn.close()
    os.unlink(tmp.name)
