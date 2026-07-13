"""daemon.py — long-running dispatcher. Wakes every N minutes, drains
pending+awaiting-quota jobs from `jobs.sqlite`, exits when told.

ponytail: stdlib only — a single while-loop with sleep. No APScheduler,
no Celery beat, no Docker cron image. The job queue carries the audit
trail; the daemon is just the trigger.

Usage:
    python bin/daemon.py --interval 20 --once        # drain once + exit
    python bin/daemon.py --interval 20m            # every 20 min until SIGINT
    python bin/daemon.py --interval 1500s --limit 10   # 25 min, batch 10

Schedule (Windows):
    schtasks /Create /SC MINUTE /MO 20 /TN vp-drain /TR \
      "C:\\path\\to\\venv\\Scripts\\python.exe bin\\daemon.py --interval 20m --logfile %LOCALAPPDATA%\\hermes\\daemon.log"

Ponytail did NOT build an "agent decides what to enqueue on every tick"
loop. The pattern there is unbounded: it hallucinates tasks, burns free-
tier quota, and pages you at 3am. Concrete frictions should land in the
queue via named `enqueue` calls (or, later, via cron-fronted CLI scripts
that enqueue based on a rule). The daemon's job is to run what was
already intentionally queued.

Behavior:
- Posts an "alive heartbeat" line to stderr every iteration so you can
  tail the log and see it's not wedged.
- Sleeps the configured interval between iterations, but completes the
  current batch first (so 20min interval with a 15min batch gives
  35min between cycles — that's a feature, not a bug).
- Reuses jobs.dispatch(limit=N) verbatim. Idempotent state-machine; a
  wedged process that gets reaped mid-batch leaves jobs in `running`
  state which the next startup can detect via STALE_AFTER_SECS.

Audit + state living in jobs.sqlite is what makes "from start to end
without break" real: kill -9 mid-dispatch is recoverable because every
state transition is in jobs_log, and the next process detects stuck
rows.
"""
from __future__ import annotations
import argparse
import logging
import signal
import sys
import time
from pathlib import Path

# add bin/ to sys.path so we can import jobs (which lives there)
BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import jobs  # noqa: E402

# Stale-row detection: a row left in `running` longer than this is
# presumed orphaned (previous daemon died). On startup we re-pend it.
STALE_AFTER_SECS = 30 * 60

# Used by signal handler to break the sleep loop cleanly.
_stop = False


def _on_signal(signum, frame):
    global _stop
    _stop = True  # next loop iteration exits


def _parse_interval(spec: str) -> float:
    """Interpret '20', '20m', '20min', '1500s', '1h' as seconds."""
    s = spec.strip().lower()
    suffix = s[-1]
    if suffix.isdigit():
        return float(s) * 60  # bare number = minutes (cron convention)
    n = float(s[:-1])
    if suffix == "s":
        return n
    if suffix in ("m",):
        return n * 60
    if suffix == "h":
        return n * 3600
    if suffix == "d":
        return n * 86400
    raise SystemExit(f"can't parse interval: {spec!r}")


def _reclaim_stale_running() -> int:
    """Bump rows stuck in `running` (previous daemon died) back to
    `pending` so the next dispatch re-runs them. Idempotent because
    the jobs table itself is the source of truth, not in-memory state."""
    cutoff = time.time() - STALE_AFTER_SECS
    c = jobs._conn()
    rows = c.execute(
        "SELECT id, updated_at FROM jobs "
        "WHERE state='running' AND updated_at < ?",
        (cutoff,),
    ).fetchall()
    for r in rows:
        jobs._set_state(c, r["id"], "pending",
                        f"reclaimed by daemon; was running, age > {STALE_AFTER_SECS}s")
    c.commit()
    c.close()
    return len(rows)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--interval", default="20m",
                   help="sleep between dispatch cycles. Accepts '20m', '1500s', '1h'")
    p.add_argument("--limit", type=int, default=10,
                   help="max jobs per cycle (default: 10)")
    p.add_argument("--once", action="store_true",
                   help="run one cycle + exit; ignores SIGINT")
    p.add_argument("--logfile", type=Path, default=None,
                   help="if set, also tee the daemon log to this file")
    args = p.parse_args()

    interval_s = _parse_interval(args.interval)
    log = logging.getLogger("daemon")
    handlers = [logging.StreamHandler(sys.stderr)]
    if args.logfile:
        handlers.append(logging.FileHandler(args.logfile, encoding="utf-8"))
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        handlers=handlers,
        force=True,
    )

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    log.info(f"daemon: started; interval={args.interval} ({interval_s}s) "
             f"limit={args.limit} once={args.once}")
    jobs.init()

    while True:
        cycle_started = time.time()
        reclaimed = _reclaim_stale_running()
        if reclaimed:
            log.info(f"daemon: reclaimed {reclaimed} stale-running rows")

        n = jobs.dispatch(limit=args.limit)
        log.info(f"daemon: dispatched {n} job(s) in "
                 f"{time.time() - cycle_started:.1f}s")

        if args.once:
            return 0
        if _stop:
            log.info("daemon: stopping on signal")
            return 0

        # ponytail: compute sleep from elapsed time so a slow batch
        # doesn't get bonus iterations. Long batch + short interval =
        # no sleep that round.
        elapsed = time.time() - cycle_started
        sleep_for = max(0.0, interval_s - elapsed)
        if sleep_for > 0:
            log.info(f"daemon: sleeping {sleep_for:.1f}s")
            # break sleep into 1s slices so SIGINT is responsive
            slept = 0.0
            while slept < sleep_for and not _stop:
                chunk = min(1.0, sleep_for - slept)
                time.sleep(chunk)
                slept += chunk


if __name__ == "__main__":
    sys.exit(main())
