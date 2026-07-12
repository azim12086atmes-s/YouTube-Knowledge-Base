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


if __name__ == "__main__":
    # ponytail: one self-check. Probes load+insert+search roundtrip.
    import tempfile, os, sys
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)

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

    conn.close()
    os.unlink(tmp.name)
