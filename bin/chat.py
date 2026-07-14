"""chat.py — multi-turn REPL over the analyzed corpus.

State:
  - Conversation: chat_messages(session_id, role, content, ts) in
    analyzed.sqlite (same file as the vector index).
  - Session: a string id (default: derived from CLI invocation; pass
    --session to switch).

Each turn:
  1. Vector search → top-k chunks via vector_store.search()
  2. Build Gemini prompt with system instructions + retrieved chunks +
     last K turns of history
  3. POST to gemini-3.1-flash-lite (text mode)
  4. Print response, save turn to history

History pruning: cap at 8 messages. Each turn the user sees how many
prior turns are in context. Drop-oldest is automatic; never summarises
(that's a real rung).

Commands (REPL):
  :quit      exit (Ctrl-D / Ctrl-Z also exits)
  :clear     wipe session history
  :status    show session id + message count + last retrieval summary
  :show      show last retrieved chunks (same as ask.py --show-chunks)
  :history   dump raw conversation history
  :sessions  list all sessions in the DB
"""

from __future__ import annotations
import argparse
import os
import sqlite3
import sys
from pathlib import Path
from _gemini import gemini_key as _gemini_key, post as _gemini_post

# ponytail: back-compat shim. Real helper lives in _gemini.py.
gemini_key = _gemini_key

GEMINI_MODEL = "gemini-3.1-flash-lite"
DEFAULT_TRANSCRIPT_DIR = Path.home() / "Documents" / "video-analysis"
HISTORY_CAP = 8  # messages kept verbatim per turn (user+model pairs)

SYSTEM_PROMPT = (
    "You are a research assistant for a personal YouTube Knowledge Base. "
    "Answer the user's question using ONLY the provided transcript excerpts. "
    "Quote specific moments (with the slug + a verbatim phrase) to support "
    "each claim. If the transcripts don't address the question, say so "
    "explicitly. Be brief and direct. The conversation history above this "
    "message is your prior context; refer to it when the user says 'as I "
    "mentioned' or asks follow-ups."
)


# ponytail: gemini_key shim — back-compat for callers importing this module.
# Real implementation lives in _gemini.py.
def gemini_key_local():
    return _gemini_key()


SYSTEM_PROMPT = (
    "You are a research assistant for a personal YouTube Knowledge Base. "
    "Answer the user's question using ONLY the provided transcript excerpts. "
    "Quote specific moments (with the slug + a verbatim phrase) to support "
    "each claim. If the transcripts don't address the question, say so "
    "explicitly. Be brief and direct. The conversation history above this "
    "message is your prior context; refer to it when the user says 'as I "
    "mentioned' or asks follow-ups."
)


def retrieve_chunks(idx_path: Path, question: str, k: int = 8,
                    allowed_slugs=None, mode: str = "hybrid") -> list[dict]:
    """Open the corpus index, run a retrieval, return hits.

    ponytail: when allowed_slugs is provided, filter hits to that set
    AFTER retrieval. fetch_k doubled so post-filter doesn't shrink the
    visible set under k. Used by :tag filter.

    mode ∈ {"dense", "hybrid", "pinpoint"}:
    - dense    : cosine-only on MiniLM embeddings (legacy default)
    - hybrid   : RRF-fused dense + FTS5 BM25 (D27, Anthropic 49% lift)
    - pinpoint : BM25-only on a quoted phrase, top-k exact matches

    ponytail: hybrid is the new recommended default for corpus-wide
    searches. Dense-only remains for callers that explicitly want it
    (e.g. e2e tests that probe the original code path).
    """
    if not idx_path.exists():
        return []
    try:
        import sqlite_vec  # lazy import
        conn = sqlite3.connect(str(idx_path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        from vector_store import (
            search as vec_search,
            hybrid_search,
            pinpoint_search,
        )
        if mode == "pinpoint":
            hits = pinpoint_search(conn, question, k=k)
            conn.close()
            return hits
        if mode == "hybrid":
            hits = hybrid_search(conn, question, k=k,
                                 allowed_slugs=allowed_slugs)
            conn.close()
            return hits
        # default: dense (legacy)
        fetch_k = k * 2 if allowed_slugs else k
        hits = vec_search(conn, question, k=fetch_k)
        if allowed_slugs is not None:
            hits = [h for h in hits if h['slug'] in allowed_slugs][:k]
        conn.close()
        return hits
    except Exception as e:
        print(f"# retrieve failed ({type(e).__name__}: {e}); "
              f"continuing without retrieval", file=sys.stderr)
        return []




# ponytail: session-scoped key/value state lives in its own table.
# Currently only `tag` is stored (active tag filter). Schema is open-ended
# so future keys (last_retrieval_mode, history_cap_override, ...) land here.
SESSION_STATE_SQL = """
    CREATE TABLE IF NOT EXISTS session_state (
        session_id TEXT NOT NULL,
        key        TEXT NOT NULL,
        value      TEXT,
        PRIMARY KEY (session_id, key)
    )
"""


def init_session_state(conn):
    conn.execute(SESSION_STATE_SQL)
    conn.commit()


def get_session_kv(conn, session_id, key):
    init_session_state(conn)
    row = conn.execute(
        "SELECT value FROM session_state WHERE session_id=? AND key=?",
        (session_id, key),
    ).fetchone()
    return row[0] if row else None


def set_session_kv(conn, session_id, key, value):
    init_session_state(conn)
    if value is None:
        conn.execute(
            "DELETE FROM session_state WHERE session_id=? AND key=?",
            (session_id, key),
        )
    else:
        conn.execute(
            "INSERT INTO session_state(session_id, key, value) VALUES(?,?,?) "
            "ON CONFLICT(session_id, key) DO UPDATE SET value=excluded.value",
            (session_id, key, value),
        )
    conn.commit()


def build_contents(history: list[dict], question: str,
                   chunks: list[dict]) -> list[dict]:
    """Assemble Gemini's `contents` array with system + history + question.

    Pattern: one initial user-message that carries the system instructions
    + retrieved context, then alternating user/model pairs for history,
    then the current user question as the final user message.
    """
    ctx_lines = []
    if chunks:
        ctx_lines.append(f"Retrieved transcript excerpts ({len(chunks)}):\n")
        for h in chunks:
            ctx_lines.append(
                f"[{h['slug']} (dist={h['distance']:.3f})]\n{h['text']}\n"
            )
    else:
        ctx_lines.append(
            "(No retrieval: corpus index missing or empty. "
            "Run analyze.py --reindex-from-md to populate embeddings.)"
        )
    corpus_block = "\n".join(ctx_lines)

    contents: list[dict] = []
    # First user message carries the system + corpus context.
    contents.append({
        "role": "user",
        "parts": [{"text": f"{SYSTEM_PROMPT}\n\n{corpus_block}"}],
    })
    # The model needs to "see" an assistant turn before the next user
    # turn — Gemini requires alternating roles. Stub it.
    contents.append({"role": "model", "parts": [{"text": "Understood."}]})

    # Conversation history.
    for m in history:
        contents.append({
            "role": m["role"],
            "parts": [{"text": m["content"]}],
        })

    # Current user question.
    contents.append({"role": "user", "parts": [{"text": question}]})
    return contents


# ponytail: thin wrapper — real POST in _gemini.py.
def call_gemini(api_key: str, contents: list[dict]) -> str:
    return _gemini_post(contents, api_key, GEMINI_MODEL)


def repl(idx_path: Path, session_id: str, k: int) -> int:
    api_key = gemini_key()
    conn = sqlite3.connect(str(idx_path))
    conn.enable_load_extension(True)
    try:
        import sqlite_vec
        sqlite_vec.load(conn)
    except Exception:
        # ponytail: if sqlite-vec extension is unavailable we fall through
        # without vector search; ask.py's bundle-and-ask fallback handles it.
        pass

    from vector_store import (load_messages, save_message, clear_session,
                             list_sessions, init_tags, get_slugs_by_tag)
    init_tags(conn)

    print(f"chat: session={session_id!r}  index={idx_path}")
    print(f"type a question. commands: :quit :clear :status :show :history :sessions :tag [name]")
    last_hits: list[dict] = []  # ponytail: shared with :show

    while True:
        try:
            line = input(f"\n[{session_id}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.startswith(":"):
            cmd = line[1:].strip().lower()
            if cmd == "quit":
                break
            if cmd == "clear":
                n = clear_session(conn, session_id)
                print(f"cleared {n} messages from session {session_id!r}")
                last_hits = []
                continue
            if cmd == "status":
                msgs = load_messages(conn, session_id, limit=10_000)
                sessions = list_sessions(conn)
                print(f"session: {session_id!r}  messages: {len(msgs)}  "
                      f"history cap: {HISTORY_CAP}")
                if last_hits:
                    print(f"last retrieval: {len(last_hits)} chunks, "
                          f"top slug: {last_hits[0]['slug']} "
                          f"(dist={last_hits[0]['distance']:.3f})")
                print(f"all sessions: {[s['session_id'] for s in sessions]}")
                continue
            if cmd == "show":
                if not last_hits:
                    print("(no retrieval yet)")
                else:
                    for i, h in enumerate(last_hits, 1):
                        print(f"\n## {i}. {h['slug']} (distance={h['distance']:.3f})")
                        print(h["text"].strip())
                continue
            if cmd == "history":
                msgs = load_messages(conn, session_id, limit=10_000)
                for m in msgs:
                    print(f"[{m['role']}] {m['content']}")
                continue
            if cmd == "sessions":
                for s in list_sessions(conn):
                    print(f"  {s['session_id']:20s} count={s['count']}")
                continue
            # ponytail: :tag [name] — set / show / clear the session tag filter.
            # Argument is everything after "tag"; empty arg shows current filter.
            if cmd.startswith("tag"):
                arg = cmd[3:].strip()
                if not arg:
                    cur = get_session_kv(conn, session_id, "tag")
                    print(f"active tag: {cur if cur else '(none)'}")
                    continue
                from vector_store import TAG_VOCAB as _vocab, get_slugs_by_tag as _g
                if arg not in _vocab:
                    print(f"unknown tag {arg!r}; valid: {', '.join(_vocab)}")
                    continue
                count = len(_g(conn, arg))
                set_session_kv(conn, session_id, "tag", arg)
                print(f"active tag set to {arg!r} ({count} slug(s) match)")
                continue
            print(f"unknown command: {cmd!r}")
            continue

        # Real turn.
        save_message(conn, session_id, "user", line)
        history = load_messages(conn, session_id, limit=HISTORY_CAP)
        # ponytail: apply the session's active tag filter to retrieval.
        # allowed=None when no filter is set; set() of slugs otherwise.
        active = get_session_kv(conn, session_id, "tag")
        allowed: set[str] | None = None
        if active:
            from vector_store import get_slugs_by_tag as _g
            allowed = _g(conn, active)
        last_hits = retrieve_chunks(idx_path, line, k=k, allowed_slugs=allowed)
        if active:
            print(f"# tag filter active: {active!r} ({len(allowed)} slug(s))",
                  file=sys.stderr)
        contents = build_contents(history, line, last_hits)
        reply = call_gemini(api_key, contents)
        if reply.startswith("ERROR"):
            print(f"# {reply}", file=sys.stderr)
            continue
        save_message(conn, session_id, "model", reply)
        print(reply)

    conn.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="multi-turn RAG chat over the corpus")
    p.add_argument("--session", default="default",
                   help="session id (default: 'default'). Different ids = different histories.")
    p.add_argument("--k", type=int, default=8,
                   help="top-k chunks to retrieve per turn (default: 8)")
    p.add_argument("--transcripts-dir", type=Path, default=DEFAULT_TRANSCRIPT_DIR,
                   help=f"directory holding <slug>.transcript.txt sidecars "
                        f"(default: {DEFAULT_TRANSCRIPT_DIR})")
    args = p.parse_args()
    idx_path = args.transcripts_dir / "analyzed.sqlite"
    return repl(idx_path, args.session, args.k)


if __name__ == "__main__":
    sys.exit(main())
