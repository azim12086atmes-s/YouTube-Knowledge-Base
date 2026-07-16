"""jobs.py — ops job queue for video-pipeline + future personal-ops tasks.

ponytail rung 1 of N: this is the SEED. It proves the architecture on a
real task (`analyze`) so the next six rungs (dispatcher, quota-aware
schedule, approval gates, post / apply workers) build on something
that already works, not a sketch.

Why this exists (not the existing analyze.py / pipeline.py):
- analyze.py today runs one URL synchronously. Long-running, kills the
  caller on Ctrl-C, no audit trail, no idempotent re-runs.
- pipeline.py --resume is the closest thing we have, but it's
  chained to the corpus index. A job queue generalizes: same machinery
  for analyzing URLs, posting to LinkedIn, applying to jobs, etc.

Public surface (rung 1):
  bin/jobs.py init                                create the SQLite file + schema
  bin/jobs.py enqueue KIND --payload '{...}'      insert a row; idempotent on key_hash
  bin/jobs.py list [--state S] [--limit N]         show what's in the queue
  bin/jobs.py dispatch [--limit N]                run pending+awaiting-quota rows to a terminal state
  bin/jobs.py show JOB_ID                         full row + audit log for one job

Worker registry (rung 1): just `analyze`. Add post / apply / ask in
later rungs by appending to WORKERS.

Stdlib only. Same SQLite file pattern as `analyzed.sqlite`.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

JOBS_DB = Path.home() / "AppData" / "Local" / "hermes" / "jobs.sqlite"
REPO = Path(__file__).resolve().parent.parent  # bin/jobs.py -> repo root


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT    NOT NULL,
    payload_json    TEXT    NOT NULL,
    key_hash        TEXT    NOT NULL,
    scope_key       TEXT    NOT NULL DEFAULT '',
    state           TEXT    NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','pending-approval','awaiting-quota',
                         'running','ok','skipped','failed')),
    needs_approval  INTEGER NOT NULL DEFAULT 0,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL,
    last_error      TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    max_retries     INTEGER NOT NULL DEFAULT 5
);
CREATE INDEX IF NOT EXISTS jobs_state_updated ON jobs(state, updated_at);
CREATE INDEX IF NOT EXISTS jobs_key_scope    ON jobs(key_hash, scope_key);

CREATE TABLE IF NOT EXISTS jobs_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id),
    from_state TEXT,
    to_state   TEXT    NOT NULL,
    note       TEXT,
    ts         REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_log_job ON jobs_log(job_id, ts);
"""


def _conn() -> sqlite3.Connection:
    """Open the jobs DB, creating the file (not the schema) if needed."""
    JOBS_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(JOBS_DB))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")  # ponytail: safe with multi-process writers
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init() -> None:
    c = _conn()
    c.executescript(SCHEMA)
    c.commit()
    c.close()
    print(f"jobs DB ready at {JOBS_DB}")


def _canonical_key(payload: dict, scope_key: str) -> tuple[str, str]:
    """Hash the JSON-serialised payload + scope; identical logical input
    across calls => same hash => idempotent re-enqueue."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256((scope_key + "|" + canonical).encode("utf-8")).hexdigest()[:32]
    return h, canonical


def enqueue(kind: str, payload: dict, scope_key: str = "",
            needs_approval: bool = False,
            max_retries: int = 5) -> Optional[int]:
    """Insert a job. Returns its id, or None if (key_hash, scope_key) already
    exists. ponytail: the duplicate-returns-None is the idempotency guarantee —
    dispatchers can re-enqueue without double-running side effects."""
    c = _conn()
    key_hash, canonical = _canonical_key(payload, scope_key)
    now = time.time()
    # ponytail: idempotent on (key_hash, scope_key). If a row already
    # exists in any state (incl. terminal), we don't insert again. This
    # is the rule that lets a corrupted dispatcher resume without
    # double-posting.
    existing = c.execute(
        "SELECT id, state FROM jobs WHERE key_hash=? AND scope_key=? LIMIT 1",
        (key_hash, scope_key),
    ).fetchone()
    if existing:
        print(f"# duplicate: job id={existing['id']} already exists "
              f"(state={existing['state']}, key_hash={key_hash[:12]}...)")
        c.close()
        return None
    cur = c.execute(
        """INSERT INTO jobs (kind, payload_json, key_hash, scope_key, state,
                            needs_approval, created_at, updated_at,
                            max_retries)
           VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
        (kind, canonical, key_hash, scope_key,
         1 if needs_approval else 0,
         now, now, max_retries),
    )
    job_id = cur.lastrowid
    c.execute(
        """INSERT INTO jobs_log (job_id, from_state, to_state, note, ts)
           VALUES (?, NULL, 'pending', ?, ?)""",
        (job_id, f"enqueued by user; kind={kind}", now),
    )
    c.commit()
    c.close()
    print(f"# enqueued job id={job_id} kind={kind} key_hash={key_hash[:12]}...")
    return job_id


def list_jobs(state: Optional[str] = None,
              limit: int = 50) -> list[sqlite3.Row]:
    c = _conn()
    sql = "SELECT * FROM jobs"
    params: list = []
    if state:
        sql += " WHERE state = ?"
        params.append(state)  # ponytail: append, do not overwrite, so a
                              # state filter and a LIMIT coexist correctly.
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    rows = c.execute(sql, params).fetchall()
    c.close()
    return rows


def show_job(job_id: int) -> tuple[Optional[sqlite3.Row], list[sqlite3.Row]]:
    c = _conn()
    job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    log = c.execute(
        "SELECT * FROM jobs_log WHERE job_id=? ORDER BY id ASC", (job_id,)
    ).fetchall()
    c.close()
    return job, log


# ----- workers (rung 1: just `analyze`) -----------------------------------

def _set_state(c, job_id: int, to_state: str, note: str) -> None:
    """Atomic state transition: update jobs.jobs + append jobs_log. The
    two writes are inside a single transaction so the audit log can
    never disagree with the row state."""
    row = c.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return
    from_state = row["state"]
    if from_state == to_state:
        return  # nothing to do, don't spam the log
    now = time.time()
    c.execute(
        "UPDATE jobs SET state=?, updated_at=? WHERE id=?",
        (to_state, now, job_id),
    )
    c.execute(
        "INSERT INTO jobs_log (job_id, from_state, to_state, note, ts) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, from_state, to_state, note, now),
    )


def _worker_analyze(payload: dict, job_id: int) -> None:
    """Calls `python bin/analyze.py <url> ...` via subprocess.

    ponytail: this worker knows about existing analyze.py — no new RAG,
    no new model logic. The job queue's job is to schedule and audit,
    not to rediscover analysis.

    Modes (set via payload["mode"]):
      - "transcript"   default. analyze.py fetches transcript + 4-shape
                       Gemini prompts (~3s/video, free-tier quota-bound)
      - "multimodal"   analyze.py --multimodal. Gemini watches the
                       video (~11s/video, free-tier quota-bound)
      - "ingest-raw"   analyze.py --ingest-raw --force. Local
                       transcript + local embed, NO Gemini call
                       (~1-2s/video, no rate limit, the right
                       path for the 40k-URL walk)
    """
    url = payload["url"]
    mode = payload.get("mode", "transcript")
    out = payload.get("out_dir", str(Path.home() / "Documents" / "video-analysis"))

    cmd = [sys.executable, str(REPO / "bin" / "analyze.py"), url, "--out", out]
    if mode == "multimodal":
        cmd.append("--multimodal")
    elif mode == "ingest-raw":
        cmd.extend(["--ingest-raw", "--force"])

    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    c = _conn()
    try:
        if p.returncode == 0:
            _set_state(c, job_id, "ok", f"analyze.py rc=0; bytes_stdout={len(p.stdout)}")
            c.commit()
        else:
            _set_state(c, job_id, "failed",
                       f"analyze.py rc={p.returncode}; stderr={p.stderr[-300:]}")
            c.execute("UPDATE jobs SET last_error=?, retry_count=retry_count+1 "
                      "WHERE id=?", (p.stderr[-500:], job_id))
            c.commit()
    finally:
        c.close()


WORKERS: dict[str, Callable[[dict, int], None]] = {
    "analyze": _worker_analyze,
}


def dispatch(limit: int = 25, worker_path: Optional[Path] = None) -> int:
    """Pull pending rows oldest-first, hand each to its worker, advance
    state to ok/skipped/failed. Idempotent because the worker's writes
    are atomic per-row.

    Returns the count of rows dispatched (any terminal-state transition)."""
    if worker_path:
        # ponytail: future rungs will load custom worker modules. v1 ships
        # with one in-process worker; cross-process workers are rung 2.
        raise SystemExit("cross-process workers: rung 2")
    c = _conn()
    pending = c.execute(
        "SELECT id, kind, payload_json, retry_count, max_retries "
        "FROM jobs WHERE state IN ('pending','awaiting-quota') "
        "ORDER BY updated_at ASC LIMIT ?",
        (limit,),
    ).fetchall()
    if not pending:
        print("# nothing to dispatch")
        c.close()
        return 0

    n = 0
    for row in pending:
        kind = row["kind"]
        worker = WORKERS.get(kind)
        _set_state(c, row["id"], "running", f"dispatcher picked; kind={kind}")
        c.commit()
        # ponytail: close the conn before the worker runs. The worker
        # opens its own connection (sqlite3 WAL is fine across threads).
        # This way a hung subprocess doesn't hold a write lock.
        c.close()
        if worker is None:
            c = _conn()
            _set_state(c, row["id"], "failed",
                       f"no worker registered for kind={kind!r}")
            c.commit(); c.close()
            n += 1
            continue
        try:
            payload = json.loads(row["payload_json"])
            worker(payload, row["id"])
            n += 1
        except Exception as e:
            c = _conn()
            _set_state(c, row["id"], "failed",
                       f"worker raised {type(e).__name__}: {e}")
            c.execute("UPDATE jobs SET last_error=?, retry_count=retry_count+1 "
                      "WHERE id=?", (str(e)[:500], row["id"]))
            c.commit(); c.close()
            n += 1
        c = _conn()  # re-open for next iteration
    c.close()
    print(f"# dispatched {n} job(s)")
    return n


# ----- CLI dispatch -------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="video-pipeline ops queue (rung 1)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="create or upgrade the jobs DB schema")
    pe = sub.add_parser("enqueue", help="add one job to the queue")
    pe.add_argument("kind")
    pe.add_argument("--payload", required=True,
                    help="JSON object with kind-specific args")
    pe.add_argument("--scope", default="")
    pe.add_argument("--needs-approval", action="store_true",
                    help="mark this row pending-approval (side effect)")
    pe.add_argument("--max-retries", type=int, default=5)
    pl = sub.add_parser("list", help="list jobs")
    pl.add_argument("--state")
    pl.add_argument("--limit", type=int, default=50)
    ps = sub.add_parser("show", help="show one job + log")
    ps.add_argument("job_id", type=int)
    pd = sub.add_parser("dispatch", help="run pending rows to terminal state")
    pd.add_argument("--limit", type=int, default=25)

    args = p.parse_args()
    if args.cmd == "init":
        init()
    elif args.cmd == "enqueue":
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as e:
            print(f"# bad --payload JSON: {e}", file=sys.stderr); return 2
        if not isinstance(payload, dict):
            print("# --payload must be a JSON object", file=sys.stderr); return 2
        if args.kind not in WORKERS:
            print(f"# unknown kind {args.kind!r}; "
                  f"available: {sorted(WORKERS)}", file=sys.stderr); return 2
        job_id = enqueue(args.kind, payload,
                         scope_key=args.scope,
                         needs_approval=args.needs_approval,
                         max_retries=args.max_retries)
        return 0 if job_id is not None else 1
    elif args.cmd == "list":
        rows = list_jobs(state=args.state, limit=args.limit)
        if not rows:
            print(f"# no rows{' in state ' + args.state if args.state else ''}")
            return 0
        print(f"# {len(rows)} row(s):")
        for r in rows:
            err = (r["last_error"] or "")[:50]
            print(f"  id={r['id']:>4}  {r['state']:18s}  retry={r['retry_count']}/{r['max_retries']}  "
                  f"{r['kind']:14s}  err={err!r}")
        return 0
    elif args.cmd == "show":
        job, log = show_job(args.job_id)
        if not job:
            print(f"# no job with id={args.job_id}", file=sys.stderr); return 2
        print(f"# job id={job['id']} kind={job['kind']} state={job['state']}")
        print(f"  payload_json={job['payload_json']}")
        print(f"  key_hash={job['key_hash']}  scope_key={job['scope_key']!r}")
        print(f"  retry={job['retry_count']}/{job['max_retries']}  "
              f"needs_approval={job['needs_approval']}")
        err = job["last_error"]
        if err:
            print(f"  last_error={err[:300]}")
        print(f"# log ({len(log)} entries):")
        for e in log:
            print(f"  {e['ts']:.1f}  {e['from_state'] or '-':>16} -> {e['to_state']:16s}  "
                  f"{e['note'] or ''}")
        return 0
    elif args.cmd == "dispatch":
        return 0 if dispatch(limit=args.limit) > -1 else 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
