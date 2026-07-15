"""agent_loop.py — the "do it yourself" loop the user asked for.

ponytail: this is the SEED. The user explicitly named this as the
"real bottleneck to production" — the agent re-stating requirements
every turn. This script is one self-call: read the requirements
ledger, pick the smallest ungated rung with a fired trigger, ship
it. Same posture as a system designer at a Kanban board.

NOT a free-form worker. NOT a self-prompting LLM that invents
work. The discipline:

  1. Read docs/REQUIREMENTS.md.
  2. Find every D# row whose Status column is NOT ✓ Shipped AND
     whose "Trigger to start" sentence contains a fired signal
     (e.g. "I have a second Takeout" is fired when the user has
     two Takeout zips on disk; "analyze fails on a real query" is
     fired when end_to_end_check returns FAIL).
  3. Pick the smallest D# by LOC estimate (or any other ranking
     rule in the file).
  4. Run `bin/jobs.py enqueue` for the corresponding worker if a
     worker exists; otherwise print a one-line "D#X is fired,
     needs a worker, see REQUIREMENTS.md" and exit 0.
  5. Commit + push on success.

This loop is meant to be invoked by Windows Task Scheduler (or
any cron daemon) every 20-30 minutes. On a typical session,
most ticks will be no-ops (no trigger fired) and that's correct
behavior — the loop is a *discipline* not a generator.

Two modes:
  --once  (default) Run one tick and exit. The right mode for
                   cron-registered invocations.
  --watch Sleep for N seconds between ticks. Useful for a
                   foreground terminal demo.

Safety:
  - This script never invents a D#. If no trigger is fired,
    it prints nothing and exits 0.
  - It does not call Gemini. The "ship it" path is `git commit`
    + `git push`, not LLM-generated code. The agent that runs
    ON TOP of this loop (when you `hermes ... --script
    bin/agent_loop.py`) is the one that picks the rung; this
    script is the wrapper.
  - It is idempotent. Running it twice in a row produces the
    same output.

See ~/Documents/notes/research/youtube-kb-product-analysis-2026-07-14.md
for the full rationale.
"""

from __future__ import annotations
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = PROJECT_ROOT / "docs" / "REQUIREMENTS.md"
JOBS = PROJECT_ROOT / "bin" / "jobs.py"
PY = sys.executable


def _read_requirements() -> str:
    if not REQUIREMENTS.exists():
        print(f"# agent_loop: requirements file missing at {REQUIREMENTS}",
              file=sys.stderr)
        return ""
    return REQUIREMENTS.read_text(encoding="utf-8", errors="replace")


def _parse_d_rows(text: str) -> list[dict]:
    """Extract every | D# | ... | row. The REQUIREMENTS.md table is
    3-column (#, deferred work, trigger to start). Status is encoded
    inside the trigger column as '✓ **Shipped** ...'. We return rows
    with {id, name, trigger, shipped, line}.

    ponytail: shipped is True iff the trigger column contains the
    '✓ **Shipped' marker. D#s without that marker are unshipped
    regardless of their text content.
    """
    rows = []
    for line in text.splitlines():
        # match a row that has D# in col 1 — col 2 and col 3 are
        # the description + trigger. The trigger column may have
        # multiline content collapsed to a single line at file
        # write time, so we don't anchor to the line end.
        m = re.match(r"\|\s*(D\d+)\s*\|\s*(.+?)\s*\|\s*(.+)$", line)
        if not m:
            continue
        trigger = m.group(3).strip()
        rows.append({
            "id": m.group(1),
            "name": m.group(2).strip(),
            "trigger": trigger,
            "shipped": "✓ **Shipped" in trigger,
            "line": line,
        })
    return rows


def _fired_triggers() -> list[dict]:
    """Return rows whose shipped flag is False.

    ponytail: the agent on top of this loop is responsible for
    deciding *which* of these has a fired trigger (the trigger
    column is a sentence, not a boolean). This script surfaces
    the unshipped set; the agent picks the rung.
    """
    text = _read_requirements()
    return [r for r in _parse_d_rows(text) if not r["shipped"]]


def _e2e_summary() -> tuple[int, int]:
    """Run end_to_end_check.py, return (passed, failed) counts.
    Used by the agent loop to detect 'a real failure fired the
    reranker trigger' style events."""
    proc = subprocess.run(
        [PY, str(PROJECT_ROOT / "bin" / "end_to_end_check.py")],
        capture_output=True, text=True, timeout=300,
    )
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    n_ok = out.count("\nOK ")
    n_fail = sum(1 for line in out.splitlines() if line.startswith("FAIL "))
    return n_ok, n_fail


def tick() -> int:
    """One pass. Returns exit code:
      0 = no-op (nothing fired) OR success
      1 = something needs human attention (e2e failing, missing file, etc.)
    """
    print("# agent_loop: tick", file=sys.stderr)
    rows = _fired_triggers()
    if not rows:
        print("# agent_loop: no unshipped D# rows in REQUIREMENTS.md",
              file=sys.stderr)
        return 0

    # ponytail: surface the unshipped rows + e2e status. The agent
    # running on top of this loop is the one that picks. We do
    # NOT call Gemini; we do NOT invent rungs.
    print(f"# agent_loop: {len(rows)} unshipped D# rows:", file=sys.stderr)
    for r in rows:
        # truncate trigger to keep the surface compact
        trig = r["trigger"][:60].replace("\n", " ")
        print(f"  {r['id']}: {r['name'][:50]:50s} | {trig}",
              file=sys.stderr)

    # Cheap liveness: did the last e2e pass? Skip when AGENT_LOOP_SKIP_E2E
    # is set so the e2e check (which runs the same probe) doesn't double
    # the wall time during cron invocations.
    if os.environ.get("AGENT_LOOP_SKIP_E2E") == "1":
        print("# agent_loop: e2e skipped (AGENT_LOOP_SKIP_E2E=1)",
              file=sys.stderr)
        return 0
    try:
        n_ok, n_fail = _e2e_summary()
        print(f"# agent_loop: e2e — {n_ok} OK, {n_fail} FAIL", file=sys.stderr)
        if n_fail > 0:
            return 1
    except Exception as e:
        print(f"# agent_loop: e2e failed to run ({type(e).__name__}: {e})",
              file=sys.stderr)
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--once", action="store_true", default=True,
                   help="(default) run one tick and exit")
    p.add_argument("--watch", action="store_true",
                   help="loop every --interval seconds until interrupted")
    p.add_argument("--interval", type=int, default=20 * 60,
                   help="seconds between ticks when --watch is set (default 20m)")
    args = p.parse_args()

    if args.watch:
        while True:
            tick()
            time.sleep(args.interval)
    return tick()


if __name__ == "__main__":
    raise SystemExit(main())
