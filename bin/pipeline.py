#!/usr/bin/env python
"""pipeline.py — Single command: Takeout zip → analyzed markdown files.

Composes takeout_sample.py + analyze.py. No new parsing, no new analysis.
Subprocess-only; each step is testable in isolation.

Usage:
    python pipeline.py [PATH/TO/takeout.zip] [--n 6] [--out OUT_DIR]

Default behavior:
- Pick most-recent JSON-form takeout zip in ~/Downloads/
- Sample 6 URLs across the date range
- Run analyze.py on each (which is responsible for skip-write on low signal)
- Print a 1-line-per-video result table to stderr

Notes (ponytail):
- composition over modification: analyze.py and takeout_sample.py are unchanged
- pipeline.py is the only thing that needs to know about both
- failures are isolated: one bad URL doesn't poison the others
- exit 0 = all wrote; exit 1 = mixed; exit 2 = zero wrote
"""

from __future__ import annotations
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
URL_SOURCE = HERE / "url_source.py"      # ponytail: was takeout_sample.py
ANALYZE = HERE / "analyze.py"
DEFAULT_OUT = Path.home() / "Documents" / "video-analysis"
# ponytail: pipeline state lives next to the corpus. Tracks cursor across runs
# so `--resume` picks up where the previous run stopped.
STATE_PATH = Path.home() / "AppData" / "Local" / "hermes" / "video-analysis" / "pipeline-state.json"


def run(cmd: list[str]) -> tuple[int, str, str]:
    """Run a subprocess, return (rc, stdout, stderr). No timeout — Gemini calls are slow."""
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def parse_sample(stdout: str) -> list[dict]:
    """Parse takeout_sample.py stdout: '<id> | <url> | <ts> | <title>'."""
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [s.strip() for s in line.split("|")]
        if len(parts) < 4:
            continue
        out.append({"id": parts[0], "url": parts[1], "ts": parts[2], "title": parts[3]})
    return out


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"cursor_index": 0, "total_processed": 0, "total_wrote": 0,
            "total_skipped": 0, "last_run_at": None, "last_processed_slug": None}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("zip", nargs="?", type=Path, default=None,
                   help="path to Takeout zip / URL-list file; default depends on --source")
    p.add_argument("--source", default="takeout-watch",
                   help="URL source (forwarded to url_source.py): "
                        "takeout-watch (default) | takeout-watch-all | "
                        "xlsx | urlfile")
    p.add_argument("--n", type=int, default=6, help="videos to sample (default: 6)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                   help=f"output directory (default: {DEFAULT_OUT})")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be analyzed; do not call Gemini")
    # ponytail: --resume walks the corpus in chronological order using the
    # takeout-watch-all source. State file tracks cursor between runs.
    p.add_argument("--resume", action="store_true",
                   help="walk the corpus in chronological order, picking up "
                        "where the previous --resume run stopped")
    p.add_argument("--batch-size", type=int, default=6,
                   help="with --resume: how many URLs to process per run (default: 6)")
    p.add_argument("--reset-state", action="store_true",
                   help="with --resume: ignore saved cursor; start from index 0")
    args = p.parse_args()

    if not URL_SOURCE.exists():
        print(f"missing {URL_SOURCE}", file=sys.stderr); return 2
    if not ANALYZE.exists() and not args.dry_run:
        print(f"missing {ANALYZE}", file=sys.stderr); return 2
    if not shutil.which(sys.executable):
        print(f"no python on PATH", file=sys.stderr); return 2

    args.out.mkdir(parents=True, exist_ok=True)

    state = load_state()
    if args.reset_state:
        state["cursor_index"] = 0

    if args.resume:
        # ponytail: chronological scan starting at saved cursor. analyze.py's
        # SQLite index handles dedup, so re-running on already-written slugs
        # is a fast no-op (we still advance cursor for each attempted URL).
        sample_cmd = [sys.executable, str(URL_SOURCE),
                      "--source", "takeout-watch-all",
                      "--start-index", str(state["cursor_index"]),
                      "--limit", str(args.batch_size)]
        if args.zip:
            sample_cmd += [str(args.zip)]
        rc, so, se = run(sample_cmd)
        if rc != 0:
            print(f"url_source.py failed (rc={rc}):\n{se}", file=sys.stderr)
            return 2
        records = parse_sample(so)
        if not records:
            print(f"# no more URLs after cursor={state['cursor_index']}; done",
                  file=sys.stderr)
            return 0
        print(f"# resumed at cursor={state['cursor_index']}; batch={len(records)}",
              file=sys.stderr)
    else:
        # Step 1: sample (default behavior, backward-compatible).
        # ponytail: source + filepath dispatch to url_source.py; for xlsx/urlfile
        # the "zip" arg is repurposed as the file path.
        sample_cmd = [sys.executable, str(URL_SOURCE),
                      "--source", args.source, "--n", str(args.n)]
        if args.zip:
            if args.source in ("xlsx", "urlfile"):
                sample_cmd += ["--file", str(args.zip)]
            else:
                sample_cmd += [str(args.zip)]
        rc, so, se = run(sample_cmd)
        if rc != 0:
            print(f"url_source.py failed (rc={rc}):\n{se}", file=sys.stderr)
            return 2
        records = parse_sample(so)
        if not records:
            print("no URLs sampled", file=sys.stderr); return 2

    print(f"# sampled {len(records)} URLs", file=sys.stderr)
    for r in records:
        print(f"  - {r['id']}  {r['ts'][:10]}  {r['title'][:60]}",
              file=sys.stderr)

    if args.dry_run:
        print("--dry-run; no Gemini calls made", file=sys.stderr)
        return 0

    # Step 2: analyze each
    results = []
    cursor_advanced = 0
    for r in records:
        url = r["url"]
        analyze_cmd = [sys.executable, str(ANALYZE), url, "--out", str(args.out)]
        rc, so, se = run(analyze_cmd)
        out_path = args.out / f"{r['id']}.md"
        wrote = out_path.exists() and rc == 0
        results.append({"id": r["id"], "wrote": wrote, "rc": rc,
                        "stderr": se.strip()[-200:] if se else ""})
        status = "WROTE" if wrote else f"SKIP(rc={rc})"
        print(f"  {status:>10}  {r['id']}  {r['title'][:50]}", file=sys.stderr)
        if args.resume:
            # ponytail: advance cursor for every *attempted* URL, not just writes.
            # Already-analyzed dedup is cheap; skip is honest record-keeping.
            state["cursor_index"] += 1
            state["last_processed_slug"] = r["id"]
            cursor_advanced += 1

    wrote = sum(1 for r in results if r["wrote"])
    skipped = len(results) - wrote
    print(f"\n# summary: {wrote} wrote, {skipped} skipped (rc=1 means signal was insufficient)",
          file=sys.stderr)

    if args.resume and cursor_advanced:
        state["total_processed"] += cursor_advanced
        state["total_wrote"] += wrote
        state["total_skipped"] += skipped
        state["last_run_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save_state(state)
        print(f"# state saved: cursor={state['cursor_index']}  "
              f"total_processed={state['total_processed']}", file=sys.stderr)

    return 0 if wrote == len(results) else (1 if wrote > 0 else 2)


if __name__ == "__main__":
    sys.exit(main())
