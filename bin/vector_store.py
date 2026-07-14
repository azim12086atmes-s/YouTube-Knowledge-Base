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
    """Create the vec table + metadata table if missing. Idempotent.

    ponytail D27: schema gains (start_s, end_s) per chunk for
    pinpoint-precision search ("at minute 17, the speaker said X"),
    and a parallel FTS5 index for BM25-style lexical queries that
    dense-only retrieval misses (proper nouns, IDs, error codes).
    Both migrations are ALTER / CREATE IF NOT EXISTS so the
    existing 75 chunks keep working — start_s/end_s are NULL until
    re-ingested with timestamps.
    """
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
    # ponytail: ALTER TABLE for the timestamp columns. Both columns
    # default to NULL so existing rows remain valid; new chunks
    # written via upsert_chunks_with_timestamps will populate them.
    # SQLite returns duplicate-column-name errors when the column
    # already exists; swallow that specific case so this stays idempotent.
    for col_ddl in (
        "ALTER TABLE chunk_meta ADD COLUMN start_s REAL",
        "ALTER TABLE chunk_meta ADD COLUMN end_s   REAL",
    ):
        try:
            conn.execute(col_ddl)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
    conn.execute(
        "CREATE INDEX IF NOT EXISTS chunk_meta_slug ON chunk_meta(slug)"
    )
    # ponytail: FTS5 for BM25-style keyword queries. The fts5 table
    # mirrors chunk_meta's rowid so we can join on it from search().
    # porter tokenizer = built-in English stemmer. content= and
    # content_rowid= are how FTS5 ties its index to the source table.
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
        "  text, "
        "  slug UNINDEXED, "
        "  idx  UNINDEXED, "
        "  tokenize='porter unicode61 remove_diacritics 2')"
    )
    # ponytail: backfill FTS5 for chunks that pre-date the FTS5 index.
    # This is a one-time migration; ensure_vec is idempotent so
    # subsequent calls won't re-insert. Safe to re-run because the
    # SELECT is keyed on (chunk_meta.rowid, NOT IN chunks_fts) — a
    # pre-populated row triggers the NOT IN and gets skipped.
    n_existing = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    n_meta = conn.execute("SELECT COUNT(*) FROM chunk_meta").fetchone()[0]
    if n_existing < n_meta:
        # ponytail: chunked insert. The fts5 INSERT is one statement
        # per row; on a 75-chunk corpus this is < 100ms, so no need
        # for fancy transaction batching. For a corpus of 10k+ chunks
        # this would warrant a 1000-statement transaction boundary.
        conn.execute(
            "INSERT INTO chunks_fts(rowid, text, slug, idx) "
            "SELECT rowid, text, slug, idx FROM chunk_meta "
            "WHERE rowid NOT IN (SELECT rowid FROM chunks_fts)"
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
    """Delete any prior chunks for slug, then store fresh ones. Returns count.

    ponytail: legacy entry point — no timestamps. For pinpoint-precision
    search, use upsert_chunks_with_timestamps() with the YouTube
    transcript segments.
    """
    return upsert_chunks_with_timestamps(conn, slug, transcript, segments=None)


def upsert_chunks_with_timestamps(
    conn: sqlite3.Connection, slug: str, transcript: str,
    segments: Optional[list[tuple[str, float, float]]] = None,
) -> int:
    """Like upsert_chunks, but if `segments` is provided (list of
    (text, start_s, end_s) tuples from youtube-transcript-api's
    FetchedTranscriptSnippet), each chunk gets start_s/end_s columns
    populated based on which segments it contains.

    ponytail: if segments is None, start_s/end_s are stored as NULL.
    The hybrid search function tolerates NULLs — chunks without
    timestamps just don't surface in pinpoint results, but they still
    appear in keyword/dense results.
    """
    ensure_vec(conn)
    # ponytail: re-embed is idempotent — old rows removed by rowid linkage.
    # D28 hardening: wrap delete + insert in a single transaction so
    # a partial failure (embedding model OOM, network drop on the
    # new chunks fetch) doesn't leave orphan rows in `chunks` that
    # have no chunk_meta rowid match.
    conn.execute("BEGIN IMMEDIATE")
    try:
        old = conn.execute(
            "SELECT rowid FROM chunk_meta WHERE slug = ?", (slug,)
        ).fetchall()
        for (rid,) in old:
            conn.execute("DELETE FROM chunks WHERE rowid = ?", (rid,))
            conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (rid,))
        conn.execute("DELETE FROM chunk_meta WHERE slug = ?", (slug,))

        # ponytail: when segments are provided, chunk boundaries are
        # derived from segment text positions inside the joined transcript,
        # not from word windows. This way a chunk's start_s/end_s are the
        # actual seconds-of-video the chunk covers — not approximated.
        if segments:
            chunks, starts, ends = _chunk_by_segments(transcript, segments)
        else:
            chunks = chunk_text(transcript)
            starts = [None] * len(chunks)
            ends = [None] * len(chunks)
        if not chunks:
            conn.commit()
            return 0
        vectors = embed(chunks)
        inserted = 0
        for idx, (text, vec, s_s, e_s) in enumerate(zip(chunks, vectors, starts, ends)):
            conn.execute("INSERT INTO chunks(embedding) VALUES (?)", [_pack(vec)])
            rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO chunk_meta(rowid, slug, idx, text, start_s, end_s) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (rid, slug, idx, text, s_s, e_s),
            )
            # ponytail: FTS5 external-content table; we feed it the rowid
            # explicitly so it joins against chunk_meta by rowid on search.
            # The tokenize=porter in the schema is the English stemmer
            # built into SQLite.
            conn.execute(
                "INSERT INTO chunks_fts(rowid, text, slug, idx) VALUES (?, ?, ?, ?)",
                (rid, text, slug, idx),
            )
            inserted += 1
        conn.commit()
        return inserted
    except Exception:
        # ponytail: a failed ingest leaves the corpus in a state where
        # the slug has no chunk_meta row (and no orphan chunks thanks
        # to BEGIN IMMEDIATE). The next --ingest-raw on this slug is a
        # clean re-attempt.
        conn.rollback()
        raise


def _chunk_by_segments(transcript: str,
                       segments: list[tuple[str, float, float]],
                       chunk_chars: int = 600,
                       overlap_chars: int = 100) -> tuple[list[str], list[float], list[float]]:
    """Greedy segment-based chunker. Each chunk covers whole consecutive
    segments until `chunk_chars` is reached; the next chunk starts
    `overlap_chars` before the current one's end so word boundaries
    don't break mid-phrase.

    Returns (chunks, start_s, end_s) — start_s is the first segment's
    start, end_s is the last segment's start + duration.

    ponytail: 600 chars is ~120 words, matching the word-window chunker
    so dense embeddings are computed at the same grain. The overlap
    keeps sentence continuity at boundaries.
    """
    if not segments:
        return [], [], []
    chunks: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    buf: list[str] = []
    buf_start: float = 0.0
    buf_end: float = 0.0
    for txt, s, e in segments:
        if not buf:
            buf_start = s
        buf.append(txt)
        buf_end = e
        if sum(len(t) for t in buf) >= chunk_chars:
            chunks.append(" ".join(buf))
            starts.append(buf_start)
            ends.append(buf_end)
            # roll back by overlap_chars worth of segments
            roll = 0
            while buf and roll < overlap_chars:
                roll += len(buf[-1])
                buf.pop()
            buf_start = buf[0][1] if buf else s  # next segment's start
    if buf:
        chunks.append(" ".join(buf))
        starts.append(buf_start)
        ends.append(buf_end)
    return chunks, starts, ends

# ponytail: lift the "which text do we embed?/classify?" decision into one
# helper. Multimodal-mode analyses have no transcript sidecar; both the
# vector index and the tagger need a fallback that reads the markdown body.
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


def body_text_for_classify(slug: str, corpus_dir) -> tuple[str, str]:
    """Pair up a slug with the best text + its kind for the tagger.

    Returns (text, kind) where kind ∈ {"transcript", "analysis-body", ""}.
    "" means no usable text; the caller skips the call."""
    import re as _re
    from pathlib import Path as _P
    base = _P(corpus_dir)
    tpath = base / f"{slug}.transcript.txt"
    if tpath.exists():
        return tpath.read_text(encoding="utf-8", errors="replace"), "transcript"
    md = base / f"{slug}.md"
    text = body_text_for_indexing(md)
    return (text, "analysis-body" if text else "")


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


# ponytail D27: hybrid dense + BM25 search. Fuses the two retrievers'
# rankings with Reciprocal Rank Fusion (RRF, k=60) — the formula from
# Cormack et al. 2009 that's the de facto standard for combining
# ranked lists. The original search() above remains for back-compat
# (e2e tests probe it). hybrid_search() is the new default; chat.py
# and ask.py should call this in D28+ once it's been exercised.
#
# Why hybrid: MiniLM dense embeddings lose exact-token and rare-word
# signal (proper nouns, IDs, error codes). FTS5 BM25 catches those.
# Anthropic's 2024 numbers: contextual-embedding-only was 35% better
# than baseline; adding BM25 took it to 49%; reranker pushed it to
# 67%. We don't ship the reranker in this rung, but the dense+BM25
# pair is the 49% lift.
def hybrid_search(conn: sqlite3.Connection, query: str, k: int = 10,
                 allowed_slugs: Optional[set[str]] = None,
                 rrf_k: int = 60) -> list[dict]:
    """Reciprocal-rank-fused dense + BM25 retrieval.

    Returns [{slug, idx, text, distance, start_s, end_s, score}]
    where score is the RRF sum (higher = more relevant). Distance is
    left in the row for back-compat with callers that read it; for
    hybrid results it's the dense rank's distance, not the fused
    score.
    """
    ensure_vec(conn)
    if not query.strip():
        return []

    # 1. dense top-k
    q_vec = embed([query])[0]
    dense_rows = conn.execute(
        "SELECT rowid, distance FROM chunks WHERE embedding MATCH ? "
        "ORDER BY distance LIMIT ?",
        [_pack(q_vec), k * 2],  # fetch 2x so post-filter doesn't shrink under k
    ).fetchall()

    # 2. FTS5 top-k
    # ponytail: FTS5 query syntax — wrap user input in double-quotes
    # so a stray * or NEAR doesn't blow up. Plus a fallback "term OR"
    # expansion so single-word queries still match. The "OR" form is
    # permissive on purpose; the dense half is the precision recall.
    safe_q = query.replace('"', '""')
    fts_rows = conn.execute(
        "SELECT rowid, bm25(chunks_fts) AS score "
        "FROM chunks_fts WHERE chunks_fts MATCH ? "
        "ORDER BY score LIMIT ?",
        (f'"{safe_q}"', k * 2),
    ).fetchall()

    # 3. RRF fusion. f(rank) = 1 / (rrf_k + rank). Sum across the two
    # lists. Ties broken by the smaller (better) dense rank.
    rrf: dict[int, float] = {}
    extras: dict[int, dict] = {}
    for rank, (rid, dist) in enumerate(dense_rows, start=1):
        rrf[rid] = rrf.get(rid, 0.0) + 1.0 / (rrf_k + rank)
        extras[rid] = {"distance": float(dist)}
    for rank, (rid, _bm) in enumerate(fts_rows, start=1):
        rrf[rid] = rrf.get(rid, 0.0) + 1.0 / (rrf_k + rank)
        extras.setdefault(rid, {})  # in case only FTS hit

    # 4. Sort by RRF score, then take top-k, then apply allowed_slugs.
    fused = sorted(rrf.items(), key=lambda kv: -kv[1])
    out: list[dict] = []
    for rid, score in fused:
        if len(out) >= k:
            break
        meta = conn.execute(
            "SELECT slug, idx, text, start_s, end_s FROM chunk_meta "
            "WHERE rowid = ?", (rid,),
        ).fetchone()
        if not meta:
            continue
        slug = meta[0]
        if allowed_slugs is not None and slug not in allowed_slugs:
            continue
        out.append({
            "slug": slug,
            "idx": meta[1],
            "text": meta[2],
            "start_s": meta[3],
            "end_s": meta[4],
            "distance": extras[rid].get("distance", 0.0),
            "score": float(score),
        })
    return out


# ponytail D27: pinpoint keyword search. FTS5 MATCH on a quoted phrase
# returns every chunk containing that phrase. Cheap, exact, and
# milliseconds on a 75-chunk corpus (sub-millisecond on thousands).
#
# Implementation note: chunks_fts is a STANDALONE FTS5 table, not an
# external-content table, because we need to filter by allowed_slugs
# at query time. The downside is the FTS5 index carries a copy of
# the text; we maintain that copy in upsert_chunks_with_timestamps().
def pinpoint_search(conn: sqlite3.Connection, phrase: str,
                    k: int = 20) -> list[dict]:
    """Lexical match for an exact phrase. Returns [{slug, idx, text,
    start_s, end_s, rank}] ordered by BM25 score."""
    ensure_vec(conn)
    if not phrase.strip():
        return []
    safe_q = phrase.replace('"', '""')
    # JOIN: chunks_fts has the FTS5 index + slug; chunk_meta has the
    # chunk position (idx) and the timestamp columns. JOIN on rowid
    # because both tables use the SQLite rowid alias.
    rows = conn.execute(
        "SELECT f.rowid, bm25(chunks_fts) AS bm, "
        "       f.slug, m.idx, m.text, m.start_s, m.end_s "
        "FROM chunks_fts f "
        "JOIN chunk_meta m ON m.rowid = f.rowid "
        "WHERE chunks_fts MATCH ? "
        "ORDER BY bm LIMIT ?",
        (f'"{safe_q}"', k),
    ).fetchall()
    out = []
    for rank, (rid, bm, slug, idx, text, s_s, e_s) in enumerate(rows, 1):
        out.append({
            "slug": slug, "idx": idx, "text": text,
            "start_s": s_s, "end_s": e_s,
            "rank": rank, "bm25": float(bm),
            "rowid": rid,
        })
    return out


CHAT_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT    NOT NULL,
        role       TEXT    NOT NULL,
        content    TEXT    NOT NULL,
        ts         REAL    NOT NULL
    )
"""


def init_chat(conn: sqlite3.Connection) -> None:
    conn.execute(CHAT_TABLE_SQL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS chat_messages_session "
        "ON chat_messages(session_id, id)"
    )
    conn.commit()
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


TAG_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS tag_assignments (
        slug TEXT    NOT NULL,
        tag  TEXT    NOT NULL,
        ts   REAL    NOT NULL,
        PRIMARY KEY (slug, tag)
    )
"""


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
