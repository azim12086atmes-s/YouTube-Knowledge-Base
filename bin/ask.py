#!/usr/bin/env python
"""ask.py — RAG over user-chosen transcripts.

Concatenates .transcript.txt sidecars for the given URLs, sends the
bundle + a user question to gemini-3.1-flash-lite (text mode), prints the
response.

Stdlib only. No new deps.

Usage:
    python ask.py URL1 URL2 URL3 --question "what did I learn about X?"
    python ask.py --urls https://youtu.be/<id> --question "..."

The transcript sidecars are produced by analyze.py (transcript mode) and
live next to each <slug>.md in ~/Documents/video-analysis/. URLs without
a sidecar are skipped with a stderr warning.
"""

from __future__ import annotations
import argparse
import json
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
# ponytail: 60 KB is the safe ceiling; transcripts above this get truncated
# (analyze.py uses the same threshold for Gemini text calls).
TRANSCRIPT_BUDGET = 60_000


def gemini_key() -> str:
    env = Path.home() / "AppData" / "Local" / "hermes" / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("GEMINI_API_KEY="):
            return line.split("=", 1)[1]
    raise SystemExit("GEMINI_API_KEY missing from ~/.hermes/.env")


def slug_from_url(u: str) -> str:
    import re
    m = re.search(r"(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})", u)
    if m:
        return m.group(1)
    if re.match(r"^[A-Za-z0-9_-]{11}$", u):
        return u
    raise SystemExit(f"could not parse YouTube ID from: {u!r}")


def load_transcripts(slugs: list[str], base: Path) -> tuple[list[str], list[str]]:
    """Returns (transcripts_in_order, missing_slugs)."""
    found, missing = [], []
    for s in slugs:
        p = base / f"{s}.transcript.txt"
        if p.exists():
            found.append(p.read_text(encoding="utf-8"))
        else:
            missing.append(s)
    return found, missing


def build_prompt(question: str, transcripts: list[str], slugs: list[str]) -> str:
    # ponytail: keep transcripts in 5-digit-char buckets per source so the
    # model has room to quote when answering.
    parts = []
    for slug, text in zip(slugs, transcripts):
        truncated = text if len(text) <= TRANSCRIPT_BUDGET else (
            text[:TRANSCRIPT_BUDGET] + "\n\n[transcript truncated at 60 KB]"
        )
        parts.append(f"--- transcript for {slug} ---\n{truncated}\n")
    bundle = "\n".join(parts)
    if len(bundle) > TRANSCRIPT_BUDGET * len(slugs):
        # ponytail: aggregate >60 KB even per-source. Trim to fit.
        bundle = bundle[:TRANSCRIPT_BUDGET * len(slugs)]
    return (
        f"You are answering a question using ONLY the YouTube transcripts "
        f"below. Quote specific moments (with the slug + a verbatim phrase) "
        f"to support each claim. If the transcripts don't address the question, "
        f"say so explicitly.\n\n"
        f"Transcripts ({len(transcripts)} videos):\n\n{bundle}\n\n"
        f"Question: {question}"
    )


def ask(question: str, transcripts: list[str], slugs: list[str],
       api_key: str) -> str:
    body = {
        "contents": [{"role": "user", "parts": [{"text": build_prompt(question, transcripts, slugs)}]}],
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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("urls", nargs="*",
                   help="YouTube URLs or 11-char video IDs (one or more)")
    p.add_argument("--all", action="store_true",
                   help="ask across every <slug>.transcript.txt in --transcripts-dir")
    p.add_argument("--question", required=True,
                   help="question to ask across the chosen transcripts")
    p.add_argument("--transcripts-dir", type=Path, default=DEFAULT_TRANSCRIPT_DIR,
                   help=f"directory holding <slug>.transcript.txt sidecars "
                        f"(default: {DEFAULT_TRANSCRIPT_DIR})")
    args = p.parse_args()

    if args.all:
        # ponytail: scan the corpus for transcripts and use them all.
        # No embedding store yet — bundle-and-ask fits corpus up to ~60 KB.
        slugs = []
        transcripts = []
        for p in sorted(args.transcripts_dir.glob("*.transcript.txt")):
            slug = p.stem
            slugs.append(slug)
            transcripts.append(p.read_text(encoding="utf-8"))
        print(f"# --all: {len(transcripts)} transcripts in {args.transcripts_dir}",
              file=sys.stderr)
    else:
        if not args.urls:
            print("error: no URLs given; pass positional URLs or --all",
                  file=sys.stderr); return 1
        slugs = [slug_from_url(u) for u in args.urls]
        transcripts, missing = load_transcripts(slugs, args.transcripts_dir)
        for m in missing:
            print(f"warn: no .transcript.txt for {m} (analyze.py transcript-mode "
                  f"creates one; multimodal-only analyses don't)", file=sys.stderr)

    if not transcripts:
        print("error: no transcripts found", file=sys.stderr); return 1

    api_key = gemini_key()

    # ponytail: retrieval is cheaper when the corpus is large. Three modes:
    #   --all            → always use vector_store.search() (top-k chunks)
    #   explicit urls    → use vector_store.search() if chunks exist for them,
    #                       fall back to bundle-and-ask otherwise
    #   small corpus     → bundle-and-ask (no retrieval overhead)
    corpus_bytes = sum(len(t) for t in transcripts)
    use_retrieval = args.all or corpus_bytes > 60_000
    if use_retrieval:
        try:
            from vector_store import search as vec_search
            from pathlib import Path as _P
            idx_path = _P(args.transcripts_dir) / "analyzed.sqlite"
            if not idx_path.exists():
                print(f"warn: no vector index at {idx_path}; "
                      f"falling back to bundle-and-ask. run analyze.py on "
                      f"transcripts to populate embeddings.", file=sys.stderr)
                use_retrieval = False
            else:
                import sqlite3
                idx_conn = sqlite3.connect(str(idx_path))
                hits = vec_search(idx_conn, args.question, k=10)
                idx_conn.close()
                if hits:
                    slugs = [h["slug"] for h in hits]
                    transcripts = [h["text"] for h in hits]
                    print(f"# ask: retrieval -> {len(hits)} chunks, "
                          f"{sum(len(t) for t in transcripts)} chars",
                          file=sys.stderr)
                else:
                    # Index has embeddings but none match — bundle fallback
                    # so the model can still see *something* from each slug.
                    print("# ask: retrieval empty; bundle-and-ask fallback",
                          file=sys.stderr)
                    use_retrieval = False
        except Exception as e:
            print(f"# ask: retrieval failed ({type(e).__name__}: {e}); "
                  f"bundle-and-ask fallback", file=sys.stderr)
            use_retrieval = False

    if not use_retrieval:
        print(f"# ask: bundle-and-ask, {len(transcripts)} transcripts, "
              f"{corpus_bytes} chars total", file=sys.stderr)

    response = ask(args.question, transcripts, slugs, api_key)
    if response.startswith("ERROR"):
        print(response, file=sys.stderr); return 2
    print(response)
    return 0


if __name__ == "__main__":
    sys.exit(main())
