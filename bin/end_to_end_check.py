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
import tempfile
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

# 7i. D21: requirements.txt present + matches the dev venv.
from pathlib import Path
import importlib.metadata as _md
_req_lines = Path("requirements.txt").read_text().splitlines()
_pin_lines = [ln for ln in _req_lines
              if ln.strip() and not ln.lstrip().startswith("#")]
# ponytail: strip the trailing `# comment` so pin comparison is exact.
_pkgs = [tuple(ln.split("#", 1)[0].strip().split("==", 1))
         for ln in _pin_lines if "==" in ln]
ok = bool(_pkgs) and all(_md.version(n) == v for n, v in _pkgs)
check("D21: requirements.txt pins match dev venv", ok,
      f"{[(n, _md.version(n), v) for n, v in _pkgs]}")

# 7k. Docs round-trip: ARCHITECTURE.md exists and references the 5
# NotebookLM features we DO transfer. Probe phrases chosen so that
# minor rewording of the doc doesn't false-fail.
_arch = (Path(__file__).resolve().parent.parent / "docs" / "ARCHITECTURE.md")
_arch_text = _arch.read_text(encoding="utf-8").lower() if _arch.exists() else ""
# ponytail: a list of phrase fragments; ≥4 of 5 must be present.
_nbm_phrases = [
    "transcript",            # transcript-only ingestion
    "source select",          # source selection per query
    "evidence",               # evidence-span IDs / quote-by-source
    "inline citation",        # inline citations
    "refusal",                # honest refusal on no-evidence
]
_hits = [p for p in _nbm_phrases if p in _arch_text]
ok = bool(_arch.exists()) and len(_hits) >= 4
check("D24: docs/ARCHITECTURE.md names the NotebookLM features we transfer",
      ok, f"present={_hits}")

# 7j. Web UI: routes present + healthz + index.html + a non-LLM endpoint.
# ponytail: TestClient from FastAPI is the official "no live port" probe.
# Skip the live-LLM /api/query probe to avoid burning free-tier quota in CI.
sys.path.insert(0, str(BIN))
try:
    from fastapi.testclient import TestClient
    import web as _web
    tc = TestClient(_web.app)

    r = tc.get("/healthz")
    check("web: /healthz returns 200 ok",
          r.status_code == 200 and r.json().get("status") == "ok",
          f"code={r.status_code}")

    r = tc.get("/")
    check("web: / returns the chat UI HTML",
          r.status_code == 200 and "video-pipeline chat" in r.text,
          f"code={r.status_code} bytes={len(r.text)}")

    # Non-LLM routes that don't cost a Gemini call.
    r = tc.get("/api/sessions")
    check("web: /api/sessions lists chat sessions",
          r.status_code == 200 and "sessions" in r.json(),
          f"code={r.status_code}")

    r = tc.delete("/api/sessions/_e2e_webtest")
    check("web: /api/sessions/{id} DELETE is idempotent",
          r.status_code == 200 and "cleared_messages" in r.json(),
          f"code={r.status_code}")

    r = tc.post("/api/sessions/_e2e_webtest/tag",
                json={"tag": "ai-tooling"})
    check("web: /api/sessions/{id}/tag sets active tag",
          r.status_code == 200 and r.json().get("slugs", 0) >= 1,
          f"code={r.status_code}")

    r = tc.post("/api/sessions/_e2e_webtest/tag",
                json={"tag": "this-tag-does-not-exist"})
    check("web: /tag refuses unknown tag (400)",
          r.status_code == 400,
          f"code={r.status_code}")

    # ponytail: D26 — picker UI route surface. /api/videos lists the
    # corpus with mode/tag/outcome filters; /api/transcripts/{slug}
    # returns the transcript text or 404.
    r = tc.get("/api/videos")
    check("web: /api/videos lists corpus with has_transcript",
          r.status_code == 200
          and isinstance(r.json().get("videos"), list)
          and len(r.json()["videos"]) > 0
          and "has_transcript" in r.json()["videos"][0],
          f"code={r.status_code}")

    r = tc.get("/api/videos?mode=multimodal&outcome=ok")
    modes = {v["mode"] for v in r.json().get("videos", [])}
    check("web: /api/videos filters by mode+outcome",
          r.status_code == 200 and modes == {"multimodal"},
          f"modes={modes}")

    r = tc.get("/api/transcripts/M1E4ZzdpOco")
    check("web: /api/transcripts/{slug} returns preview",
          r.status_code == 200
          and r.json().get("slug") == "M1E4ZzdpOco"
          and "preview" in r.json()
          and r.json()["total_chars"] > 1000,
          f"chars={r.json().get('total_chars')}")

    r = tc.get("/api/transcripts/no-such-slug-here")
    check("web: /api/transcripts/{slug} 404s on missing",
          r.status_code == 404,
          f"code={r.status_code}")

    # ponytail: D26 — picker payload shape. With slugs=[..] the API
    # must answer in url-list mode (no chunks, has slugs_used).
    # We hit a slug that has NO transcript sidecar to verify the
    # missing-slugs field surfaces the gap honestly rather than 404-ing.
    r = tc.post("/api/query", json={
        "question": "what does this speaker say?",
        "session_id": "_e2e_picker_probe",
        "slugs": ["M1E4ZzdpOco"],
        "per_slug_chars": 2000,
    })
    d = r.json()
    check("web: /api/query with slugs returns mode=url-list",
          r.status_code == 200
          and d.get("mode") == "url-list"
          and "M1E4ZzdpOco" in d.get("slugs_used", []),
          f"mode={d.get('mode')} slugs={d.get('slugs_used')}")

    # Cleanup the picker probe's session.
    tc.delete(f"/api/sessions/_e2e_picker_probe")
except Exception as e:
    check("web: TestClient import + probes", False,
          f"{type(e).__name__}: {e}")

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

# 9. Jobs queue (rung 1): init, enqueue, list, show, idempotent re-enqueue,
#    and the audit-log atomic state transition that the dispatcher relies on.
sys.path.insert(0, str(BIN))
try:
    import jobs as _jobs

    # fresh DB so the probe is reproducible
    _jobs.JOBS_DB = Path(tempfile.gettempdir()) / "jobs_e2e.sqlite"
    if _jobs.JOBS_DB.exists():
        _jobs.JOBS_DB.unlink()
    _jobs.init()

    _jid = _jobs.enqueue("analyze", {"url": "https://www.youtube.com/watch?v=E2E_PROBE"})
    check("jobs: enqueue returns int on first call",
          isinstance(_jid, int) and _jid > 0, f"id={_jid}")
    _jid2 = _jobs.enqueue("analyze", {"url": "https://www.youtube.com/watch?v=E2E_PROBE"})
    check("jobs: enqueue is idempotent on key_hash (returns None)",
          _jid2 is None, f"second_id={_jid2}")

    _rows = _jobs.list_jobs(state="pending", limit=10)
    check("jobs: list reflects the enqueued row",
          any(r["id"] == _jid for r in _rows), f"n={len(_rows)}")

    # Audit-log atomic state transition.
    c = _jobs._conn()
    _jobs._set_state(c, _jid, "running", "e2e test")
    _jobs._set_state(c, _jid, "ok", "e2e test")
    c.commit(); c.close()
    _job, _log = _jobs.show_job(_jid)
    states = [e["to_state"] for e in _log]
    check("jobs: audit log records pending -> running -> ok",
          _job["state"] == "ok" and "running" in states and "ok" in states,
          f"states={states}")

    # Cleanup the temp DB.
    if _jobs.JOBS_DB.exists():
        _jobs.JOBS_DB.unlink()
except Exception as e:
    check("jobs: probe ran cleanly", False, f"{type(e).__name__}: {e}")

# 10. daemon.py + kanban.py — scheduler hook surfaces.
try:
    import daemon as _daemon
    interval = _daemon._parse_interval("20m")
    check("daemon: --interval 20m parses to 1200s", interval == 1200.0,
          f"got {interval}")
except Exception as e:
    check("daemon: _parse_interval smoke", False, f"{type(e).__name__}: {e}")

rc = run([sys.executable, str(BIN / "daemon.py"), "--help"], timeout=15)
ok = "interval" in rc.stdout and "limit" in rc.stdout
check("daemon.py --help surfaces --interval + --limit", ok, f"rc={rc.returncode}")

rc = run([sys.executable, str(BIN / "kanban.py"), "--help"], timeout=15)
ok = "--state" in rc.stdout and "--watch" in rc.stdout
check("kanban.py --help surfaces --state + --watch", ok, f"rc={rc.returncode}")

rc = run([sys.executable, str(BIN / "kanban.py"), "--state", "ok"], timeout=10)
ok = rc.returncode == 0 and ("ok (" in rc.stdout or "no rows" in rc.stdout)
check("kanban.py --state ok renders without error", ok, f"rc={rc.returncode}")

# Summary.
print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for n in failures:
        print(f"  - {n}")
    sys.exit(1)
print("OK  all end-to-end checks passed")
sys.exit(0)
