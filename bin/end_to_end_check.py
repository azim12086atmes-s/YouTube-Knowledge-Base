#!/usr/bin/env python
"""end_to_end_check.py — one runnable check that the public pipeline works.

Why this exists:
  Ponytail rule: non-trivial logic leaves a runnable check. The chat /
  ask / analyze pipeline is non-trivial. README promises "drop a Takeout,
  run ask.py, get an answer." This file proves that promise on the current
  corpus (3 transcripts already analyzed, 111 chunks indexed across 8 files
  — multimodal-mode files are now embedded too, see D19).

What it does:
  1. Asserts the corpus directory has the expected transcripts and markdown.
  2. Asserts the SQLite vector index has chunks.
  3. Runs `analyze.py --help` and asserts the surface is correct.
  4. Runs `ask.py --all --question "..."` and asserts the answer mentions
     a word that's actually in the corpus (proving retrieval + LLM call work).
  5. Runs `chat.py --help` and asserts the CLI surface.

NOT a test framework. No fixtures. No mocks. Just subprocess + assertions.
Run from anywhere with `python bin/end_to_end_check.py`.

Exit 0 if all checks pass, non-zero otherwise. Prints a per-check summary.
"""

from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path

USERPROFILE = Path(os.environ["USERPROFILE"])
REPO = USERPROFILE / "projects" / "video-pipeline"
BIN = REPO / "bin"
CORPUS = REPO / "corpus"  # junction
IDX = CORPUS / "analyzed.sqlite"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"OK   {name}{(' — ' + detail) if detail else ''}")
    else:
        print(f"FAIL {name}{(' — ' + detail) if detail else ''}")
        failures.append(name)


def run(cmd: list[str], timeout: int = 60, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, **kw)


# 1. Corpus files.
expected_md = ["M1E4ZzdpOco.md", "wgOOBW3CJIY.md", "dEOLAltkFNU.md"]
present = [p.name for p in CORPUS.glob("*.md")]
missing = [n for n in expected_md if n not in present]
check("corpus: 3 markdown files", not missing,
      f"missing: {missing}" if missing else f"have: {present}")

expected_tx = ["M1E4ZzdpOco.transcript.txt", "wgOOBW3CJIY.transcript.txt",
               "dEOLAltkFNU.transcript.txt"]
tx_present = [p.name for p in CORPUS.glob("*.transcript.txt")]
tx_missing = [n for n in expected_tx if n not in tx_present]
check("corpus: 3 transcript sidecars", not tx_missing,
      f"missing: {tx_missing}" if tx_missing else f"have: {tx_present}")

# 2. Vector index populated.
try:
    import sqlite3, sqlite_vec
    conn = sqlite3.connect(str(IDX))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_meta = conn.execute("SELECT COUNT(*) FROM chunk_meta").fetchone()[0]
    conn.close()
    check("vector index: chunks > 0", n_chunks > 0,
          f"chunks={n_chunks} meta={n_meta}")
    check("vector index: chunks == meta", n_chunks == n_meta,
          f"chunks={n_chunks} meta={n_meta}")
except Exception as e:
    check("vector index: connects", False, str(e))

# 3. CLI surfaces exist.
for script, must_have in [
    ("analyze.py", "--transcript"),
    ("ask.py", "--all"),
    ("chat.py", "--session"),
    ("pipeline.py", "--resume"),
    ("list.py", "--tag"),
    ("vector_store.py", "OK  vector_store self-check passed"),
]:
    rc = run([sys.executable, str(BIN / script), "--help"])
    # vector_store.py takes no --help; probe with its self-check instead.
    if script == "vector_store.py":
        rc = run([sys.executable, str(BIN / script)], timeout=120)
        ok = rc.returncode == 0 and must_have in rc.stdout
        check(f"{script}: surface", ok, f"out={rc.stdout[-100:]}")
    else:
        ok = rc.returncode == 0 and must_have in rc.stdout
        check(f"{script}: surface", ok, f"out={rc.stdout[-100:] if rc.stdout else rc.stderr[-100:]}")

# 4. End-to-end: ask.py --all returns a substantive answer.
rc = run([sys.executable, str(BIN / "ask.py"), "--all",
          "--question", "What did the speaker say about conscience?"],
         timeout=120)
combined = (rc.stdout or "") + "\n" + (rc.stderr or "")
ok = (rc.returncode == 0 and "conscience" in combined.lower()
      and "wgOOBW3CJIY" in combined)
check("ask.py --all: real answer with citation",
      ok, f"rc={rc.returncode} out_len={len(rc.stdout)}")

# 5. --show-chunks surfaces raw excerpts.
rc = run([sys.executable, str(BIN / "ask.py"), "--all",
          "--question", "war and conscience",
          "--show-chunks"], timeout=120)
ok = rc.returncode == 0 and "# retrieved chunks:" in rc.stdout \
    and "distance=" in rc.stdout
check("ask.py --show-chunks: emits chunks block",
      ok, f"rc={rc.returncode}")

# 5b. ask.py --tag filter (D3).
rc = run([sys.executable, str(BIN / "ask.py"), "--all",
          "--tag", "ai-tooling",
          "--question", "What does this speaker say about building software?"],
         timeout=120)
ok = (rc.returncode == 0
      and "--tag {ai-tooling} matched" in (rc.stderr or "")
      and "M1E4ZzdpOco" in (rc.stdout or ""))
check("ask.py --tag: filters to matching slugs",
      ok, f"rc={rc.returncode} out_len={len(rc.stdout)}")

# 5c. ask.py --tag with no matches → empty + clean error.
rc = run([sys.executable, str(BIN / "ask.py"), "--all",
          "--tag", "religion-or-faith",
          "--question", "anything"], timeout=60)
ok = (rc.returncode != 0  # exits non-zero on empty result
      and "no transcripts found" in (rc.stderr or ""))
check("ask.py --tag with no matches: clean error",
      ok, f"rc={rc.returncode}")

# 6. chat.py REPL: single-turn via stdin.
import time
session = f"e2e_check_{int(time.time())}"
inp = "What did the speaker say about conscience?\n:quit\n"
rc = subprocess.run([sys.executable, str(BIN / "chat.py"),
                     "--session", session], input=inp,
                    capture_output=True, text=True, timeout=120)
ok = rc.returncode == 0
# Check persistence.
import sqlite3
conn = sqlite3.connect(str(IDX))
n = conn.execute(
    "SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (session,)
).fetchone()[0]
conn.close()
check(f"chat.py: persisted {n} messages for new session", n == 2,
      f"rc={rc.returncode}")

# 7. Clean up the test session.
conn = sqlite3.connect(str(IDX))
conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session,))
conn.commit()
conn.close()

# 7b. chat.py :tag filter (D4 REPL parity with ask.py --tag).
session_tag = f"e2e_tag_{int(time.time())}"
tag_input = (":tag ai-tooling\n:tag\nwho am i\n:quit\n")
rc = subprocess.run([sys.executable, str(BIN / "chat.py"),
                     "--session", session_tag], input=tag_input,
                    capture_output=True, text=True, timeout=60)
# Two asserts: (a) :tag ai-tooling set succeeded; (b) retrieval honor print
# mentions the filter. (We don't run a real Gemini call here — only the
# REPL commands — to keep the check fast.)
ok = (rc.returncode == 0
      and "active tag set to 'ai-tooling'" in rc.stdout
      and "tag filter active: 'ai-tooling'" in rc.stderr)
check("chat.py: :tag filter applies during retrieval", ok,
      f"rc={rc.returncode} stdout_excerpt={rc.stdout[:200]!r}")
conn = sqlite3.connect(str(IDX))
conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_tag,))
conn.execute("DELETE FROM session_state WHERE session_id = ?", (session_tag,))
conn.commit(); conn.close()

# 7c. _gemini.py shared helper — module imports + key parses + surfaces.
sys.path.insert(0, str(BIN))
try:
    import _gemini
    ok = (hasattr(_gemini, "post_text")
          and hasattr(_gemini, "post")
          and hasattr(_gemini, "gemini_key")
          and "GEMINI_API_KEY=" in Path(_gemini.ENV_PATH).read_text(encoding="utf-8"))
    check("_gemini.py: shared POST helper surface", ok,
          f"has: post_text={hasattr(_gemini, 'post_text')} "
          f"post={hasattr(_gemini, 'post')}")
except Exception as e:
    check("_gemini.py: imports", False, f"{type(e).__name__}: {e}")

# 7d. analyze.py --reclassify-from-md is exposed (idempotency: skip-if-tagged
# prevents burning free-tier quota in CI).
rc = run([sys.executable, str(BIN / "analyze.py"), "--help"], timeout=30)
ok = "--reclassify-from-md" in rc.stdout and "--retry-skips" in rc.stdout
check("analyze.py: --reclassify-from-md + --retry-skips exposed", ok,
      f"rc={rc.returncode}")

# 7e. verify at least one multimodal-mode file is now tagged (D14 effect).
n_tagged_multimodal = sqlite3.connect(str(IDX)).execute(
    "SELECT COUNT(*) FROM analyzed_videos v JOIN tag_assignments t "
    "ON v.slug=t.slug WHERE v.mode='multimodal'"
).fetchone()[0]
check("corpus: ≥1 multimodal-mode file tagged", n_tagged_multimodal >= 1,
      f"count={n_tagged_multimodal}")

# 7f. takeout_sample.py back-compat shim — still routes to url_source.py.
rc = run([sys.executable, str(BIN / "takeout_sample.py"),
          "--source", "takeout-watch", "--n", "1"], timeout=30)
ok = (rc.returncode == 0
      and "source: takeout-watch" in rc.stderr
      and "| https://www.youtube.com/watch?v=" in rc.stdout)
check("takeout_sample.py: back-compat shim works", ok,
      f"rc={rc.returncode}")

# 7g. D19: every successful analysis is in the vector index. If a markdown
# file exists with outcome='ok' but zero chunks, retrieval is silently
# blind to it — the user-facing bug the fix prevents.
try:
    import sqlite_vec as _vec
    _conn = sqlite3.connect(str(IDX))
    _conn.enable_load_extension(True)
    _vec.load(_conn)
    blind = _conn.execute(
        "SELECT v.slug FROM analyzed_videos v "
        "WHERE v.outcome='ok' AND NOT EXISTS "
        "(SELECT 1 FROM chunk_meta m WHERE m.slug=v.slug)"
    ).fetchall()
    n_blind = len(blind)
    check("D19: every ok-file is in the vector index", n_blind == 0,
          f"blind slugs: {[r[0] for r in blind]}")
    _conn.close()
except Exception as e:
    check("D19: vector index reachable", False, f"{type(e).__name__}: {e}")

# 7h. D20: backfill_watched_at script surface + --watched-at CLI on analyze.
import re as _re
n_unknown_after = sum(
    1 for f in CORPUS.glob("*.md")
    if (m := _re.search(r"^watched_at:\s*(.*)$",
                        f.read_text(encoding="utf-8"), _re.M))
    and m.group(1).strip() in ("", "unknown")
)
check("D20: watched_at backfilled (corpus has ≤1 'unknown' left)", n_unknown_after <= 1,
      f"unknown front-matter count: {n_unknown_after}")

rc = run([sys.executable, str(BIN / "analyze.py"), "--help"], timeout=15)
ok = "--watched-at" in rc.stdout
check("analyze.py: --watched-at exposed", ok,
      f"rc={rc.returncode}")

# 8. list.py: corpus inventory. (D12.)
rc = run([sys.executable, str(BIN / "list.py")], timeout=30)
ok = (rc.returncode == 0 and "M1E4ZzdpOco" in rc.stdout
      and "wgOOBW3CJIY" in rc.stdout
      and "ok" in rc.stdout)
check("list.py: surfaces corpus + tags", ok,
      f"rc={rc.returncode} out_len={len(rc.stdout)}")

rc = run([sys.executable, str(BIN / "list.py"), "--mode", "transcript"],
         timeout=30)
ok = rc.returncode == 0 and "multimodal" not in rc.stdout
check("list.py --mode transcript: filters", ok,
      f"rc={rc.returncode}")

rc = run([sys.executable, str(BIN / "list.py"),
          "--tag", "ai-tooling", "--tag", "history-or-politics"],
         timeout=30)
ok = (rc.returncode == 0
      and "M1E4ZzdpOco" in rc.stdout
      and "wgOOBW3CJIY" in rc.stdout
      and "9nAB-AC5ngE" not in rc.stdout)
check("list.py --tag (union): filters", ok,
      f"rc={rc.returncode}")

rc = run([sys.executable, str(BIN / "list.py"), "--outcome", "skip-junk"],
         timeout=30)
ok = (rc.returncode == 0
      and "skip-junk" in rc.stdout
      and "ok" not in rc.stdout.split("# ok")[-1] if "# ok" in rc.stdout else True)
check("list.py --outcome: filters", ok,
      f"rc={rc.returncode}")

# Summary.
print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for n in failures:
        print(f"  - {n}")
    sys.exit(1)
print("OK  all end-to-end checks passed")
sys.exit(0)
