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
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
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


def gemini_key() -> str:
    env = Path.home() / "AppData" / "Local" / "hermes" / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("GEMINI_API_KEY="):
            return line.split("=", 1)[1]
    raise SystemExit("GEMINI_API_KEY missing from ~/.hermes/.env")


def retrieve_chunks(idx_path: Path, question: str, k: int = 8) -> list[dict]:
    """Open the corpus index, run a vector search, return hits."""
    if not idx_path.exists():
        return []
    try:
        import sqlite_vec  # lazy import
        conn = sqlite3.connect(str(idx_path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        from vector_store import search as vec_search
        hits = vec_search(conn, question, k=k)
        conn.close()
        return hits
    except Exception as e:
        print(f"# retrieve failed ({type(e).__name__}: {e}); "
              f"continuing without retrieval", file=sys.stderr)
        return []


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


def call_gemini(api_key: str, contents: list[dict]) -> str:
    body = {
        "contents": contents,
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4096},
    }
    req = urllib.request.Request(
        f"{GEMINI_URL}?key={api_key}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
            parts = data["candidates"][0]["content"].get("parts") or []
            return "".join(p.get("text", "") for p in parts) or "(empty response)"
    except urllib.error.HTTPError as e:
        return f"ERROR {e.code}: {e.read().decode()[:300]}"
    except Exception as e:
        return f"ERROR {type(e).__name__}: {e}"


def repl(idx_path: Path, session_id: str, k: int) -> int:
    api_key = gemini_key()
    conn = sqlite3.connect(str(idx_path))
    conn.enable_load_extension(True)
    try:
        import sqlite_vec
        sqlite_vec.load(conn)
    except Exception:
        pass

    from vector_store import load_messages, save_message, clear_session, list_sessions

    print(f"chat: session={session_id!r}  index={idx_path}")
    print(f"type a question. commands: :quit :clear :status :show :history :sessions")
    last_hits: list[dict] = []

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
            print(f"unknown command: {cmd!r}")
            continue

        # Real turn.
        save_message(conn, session_id, "user", line)
        history = load_messages(conn, session_id, limit=HISTORY_CAP)
        last_hits = retrieve_chunks(idx_path, line, k=k)
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
