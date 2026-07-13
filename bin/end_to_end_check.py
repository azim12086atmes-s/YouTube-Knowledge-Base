#!/usr/bin/env python
"""end_to_end_check.py — one runnable check that the public pipeline works.

Why this exists:
  Ponytail rule: non-trivial logic leaves a runnable check. The chat /
  ask / analyze pipeline is non-trivial. README promises "drop a Takeout,
  run ask.py, get an answer." This file proves that promise on the current
  corpus (3 transcripts already analyzed, 75 chunks indexed).

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

# Summary.
print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for n in failures:
        print(f"  - {n}")
    sys.exit(1)
print("OK  all end-to-end checks passed")
sys.exit(0)
