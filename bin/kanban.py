"""kanban.py — text-format Kanban of every job in jobs.sqlite.

Columns: pending | pending-approval | awaiting-quota | running | ok | skipped | failed

Each column lists rows newest-first with id, kind, key_hash prefix,
and a note column (last 60 chars of last_error for failed; payload URL
for analyze).

Usage:
    python bin/kanban.py                       # one-shot, all jobs
    python bin/kanban.py --state failed        # only failed rows
    python bin/kanban.py --limit 200          # cap total rows shown
    python bin/kanban.py --watch --interval 5 # refresh every 5s; Ctrl-C to quit

Stdlib only. Designed for `tail -f` and SSH. If you want a web
Kanban, that's rung 2; build it on top of the existing /api/sessions
shape, do not fork this script.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import jobs  # noqa: E402

# ponytail: column order matches the lifecycle diagram in the README.
COLUMNS = ("pending", "pending-approval", "awaiting-quota",
           "running", "ok", "skipped", "failed")


def _render_row(r) -> str:
    """One cell of the kanban: 'id=X kind=analyze k=abc... | note'."""
    key = r["key_hash"][:8] if r["key_hash"] else "????????"
    err = r["last_error"] or ""
    if err:
        note = err.replace("\n", " ")[:60]
    else:
        try:
            payload = json.loads(r["payload_json"])
        except Exception:
            payload = {}
        note = payload.get("url", payload.get("topic",
                    json.dumps(payload)[:50] if payload else ""))
    line = f"  id={r['id']:>3}  {r['kind']:<14s}  k={key:<8s}  retry={r['retry_count']}/{r['max_retries']}"
    if note:
        line += f"  | {note}"
    return line


def _render_column(state: str, rows) -> str:
    out = [f"── {state} ({len(rows)}) ──"]
    for r in rows[:20]:  # cap per column so a backlogged "ok" doesn't drown the view
        out.append(_render_row(r))
    if len(rows) > 20:
        out.append(f"  ... +{len(rows) - 20} more (raise --limit to see all)")
    return "\n".join(out)


def render() -> str:
    rows = jobs.list_jobs(limit=10_000)
    by_state: dict[str, list] = {s: [] for s in COLUMNS}
    for r in rows:
        if r["state"] in by_state:
            by_state[r["state"]].append(r)
        else:
            by_state.setdefault(r["state"], []).append(r)

    out = []
    title = f"video-pipeline Kanban @ {time.strftime('%Y-%m-%d %H:%M:%S')}"
    total = len(rows)
    ok_count = len(by_state["ok"])
    fail_count = len(by_state["failed"])
    skip_count = len(by_state["skipped"])
    out.append(title)
    out.append(f"total={total}  ok={ok_count}  fail={fail_count}  skip={skip_count}  "
               f"pending={len(by_state['pending'])}")
    out.append("")
    for col in COLUMNS:
        out.append(_render_column(col, by_state[col]))
        out.append("")
    # ponytail: include any unknown states too, just in case the schema
    # ever grows a new one. Defensive but cheap.
    extras = set(by_state) - set(COLUMNS)
    for col in extras:
        out.append(_render_column(col, by_state[col]))
        out.append("")
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state", choices=COLUMNS,
                   help="only show one column")
    p.add_argument("--limit", type=int, default=10_000,
                   help="max rows to pull")
    p.add_argument("--watch", action="store_true",
                   help="refresh in place; --interval seconds between frames")
    p.add_argument("--interval", type=int, default=5,
                   help="with --watch: seconds between refreshes")
    args = p.parse_args()

    if args.state:
        rows = jobs.list_jobs(state=args.state, limit=args.limit)
        print(_render_column(args.state, rows))
        return 0

    if not args.watch:
        print(render())
        return 0

    try:
        while True:
            # ponytail: clear screen + render. Doesn't try to be clever
            # with cursor addressing — the goal is readable over SSH, not
            # pretty on a desktop terminal.
            print("\033[2J\033[H" + render(), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
