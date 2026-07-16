"""ingest_takeout.py — one-shot walker: enqueue every not-yet-analyzed
URL from every takeout-*.zip in ~/Downloads/ into the jobs queue
for `--ingest-raw` processing (no Gemini, no rate-limit quota
binding on the analysis side; YouTube's caption-API rate is the
only binding constraint).

ponytail: the user's stated pain is "make sure all videos from
takeout are analyzed by our pipeline rather than just stopping
at a few." This script is the *capability* — it walks every
takeout zip, dedupes against the existing corpus index, and
enqueues the rest. Re-runs are idempotent (jobs.py enqueue
keys on key_hash). Network is the only cost.

By default the walker enqueues and stops. With --dispatch it
also runs the dispatcher in the same shell, so a single command
drains the queue. With --rate N it sleeps 1s every N requests
to stay under YouTube's unauthenticated caption API rate limit
(~200 req/min shared).

Stdlib only. No new deps.

Usage:
  # enqueue everything not yet analyzed
  python bin/ingest_takeout.py enqueue

  # enqueue + dispatch (drains the queue in one shell)
  python bin/ingest_takeout.py walk --rate 5

  # enqueue a specific zip only
  python bin/ingest_takeout.py enqueue --zip ~/Downloads/takeout-20250508T060843Z-001.zip

  # status: how many URLs are in the takeout files vs analyzed
  python bin/ingest_takeout.py status
"""

from __future__ import annotations
import argparse
import json
import sqlite3
import sys
import time
import zipfile
from pathlib import Path
from typing import Iterable, Optional

DOWNLOADS = Path.home() / "Downloads"
CORPUS_DIR = Path("C:/Users/karee/Documents/video-analysis")
INDEX_DB = CORPUS_DIR / "analyzed.sqlite"
JOBS_DB = Path.home() / "AppData" / "Local" / "hermes" / "video-analysis" / "jobs.sqlite"
SLUG_LEN = 11
SLUG_RE = __import__("re").compile(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})")


def _extract_video_id(url: str) -> Optional[str]:
    m = SLUG_RE.search(url or "")
    return m.group(1) if m else None


def _iter_takeout_zips(downloads: Path = DOWNLOADS) -> Iterable[Path]:
    return sorted(downloads.glob("takeout-*.zip"))


def _iter_zip_urls(zip_path: Path) -> Iterable[str]:
    """Yield every youtube.com/watch?v=... URL from a Takeout zip.

    ponytail: Takeout zips put watch-history.json under
    Takeout/YouTube/history/watch-history.json. The path
    inside the zip varies by Google account; match on
    'watch-history.json' under any path. Some users have
    multiple such files (Personal + Brand accounts in the
    same export); we union them.

    Older Takeout zips (pre-2024) put the same data as
    watch-history.html. We do NOT parse HTML here — that's
    a separate rung. status() surfaces which zips are HTML-
    only so the user can decide whether to re-export.
    """
    seen = set()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.endswith("watch-history.json"):
                continue
            if "/YouTube/" not in name and "youtube" not in name.lower():
                continue
            try:
                data = json.loads(zf.read(name))
            except Exception:
                continue
            if not isinstance(data, list):
                continue
            for row in data:
                url = (row or {}).get("titleUrl") or ""
                if "youtube.com/watch" not in url:
                    continue
                vid = _extract_video_id(url)
                if vid and len(vid) == SLUG_LEN and vid not in seen:
                    seen.add(vid)
                    yield vid


def _zip_format(zp: Path) -> str:
    """Return 'json', 'html', 'mixed', or 'empty' for a Takeout zip."""
    with zipfile.ZipFile(zp) as zf:
        names = zf.namelist()
    has_json = any(n.endswith("watch-history.json") for n in names)
    has_html = any(n.endswith("watch-history.html") for n in names)
    if has_json and has_html:
        return "mixed"
    if has_json:
        return "json"
    if has_html:
        return "html"
    return "empty"


def _already_analyzed() -> set[str]:
    """Return the set of slugs the corpus index says are done."""
    if not INDEX_DB.exists():
        return set()
    c = sqlite3.connect(str(INDEX_DB))
    try:
        return {r[0] for r in c.execute("SELECT slug FROM analyzed_videos")
                if r[0]}
    finally:
        c.close()


def status() -> int:
    """Print how many unique URLs are in the takeout files vs
    how many are already analyzed."""
    if not _iter_takeout_zips():
        print(f"# no takeout-*.zip files found in {DOWNLOADS}")
        return 0
    total = 0
    by_zip: dict[Path, int] = {}
    union: set[str] = set()
    formats: dict[Path, str] = {}
    for zp in _iter_takeout_zips():
        formats[zp] = _zip_format(zp)
        n = 0
        for vid in _iter_zip_urls(zp):
            n += 1
            union.add(vid)
        by_zip[zp] = n
        total += n
    analyzed = _already_analyzed()
    pending = sorted(union - analyzed)
    print(f"# takeout files found: {len(by_zip)}")
    for zp, n in by_zip.items():
        marker = "" if formats[zp] == "json" else f"  [format: {formats[zp]}]"
        print(f"  {zp.name}: {n} unique URLs{marker}")
    html_only = [zp.name for zp, f in formats.items() if f == "html"]
    if html_only:
        print(f"# ⚠ {len(html_only)} zip(s) are HTML-only (older Takeout format).")
        print(f"#   This script does not parse HTML; re-export those zips from")
        print(f"#   Google Takeout, or extend _iter_zip_urls to parse HTML.")
    print(f"# union of unique URLs across JSON zips: {len(union)}")
    print(f"# already analyzed: {len(analyzed & union)}")
    print(f"# not yet analyzed: {len(pending)}")
    if pending and len(pending) <= 20:
        print(f"  first 20: {pending[:20]}")
    return 0


def enqueue(only_zip: Optional[Path] = None) -> int:
    """Enqueue every not-yet-analyzed URL into jobs.sqlite.

    Reads from the corpus index to know what's already done.
    Each enqueue is idempotent on key_hash. The --ingest-raw
    worker is what gets invoked when jobs dispatch; the
    worker already does NOT call Gemini.
    """
    if not _iter_takeout_zips():
        print(f"# no takeout-*.zip files in {DOWNLOADS}", file=sys.stderr)
        return 1
    analyzed = _already_analyzed()
    union: set[str] = set()
    for zp in _iter_takeout_zips():
        if only_zip and zp != only_zip:
            continue
        for vid in _iter_zip_urls(zp):
            union.add(vid)
    pending = sorted(union - analyzed)
    print(f"# enqueue: {len(pending)} URLs not yet analyzed",
          file=sys.stderr)
    if not pending:
        print("# enqueue: nothing to do", file=sys.stderr)
        return 0
    # Import the queue lazily so this script stays usable even
    # if jobs.py is broken (e.g. schema migration in flight).
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import jobs as _jobs  # noqa: E402
    if not _jobs.JOBS_DB.exists():
        _jobs.init()
    n = 0
    for vid in pending:
        url = f"https://www.youtube.com/watch?v={vid}"
        kind = "analyze"  # only worker kind; mode in payload drives the path
        payload = {"url": url, "mode": "ingest-raw", "out_dir": str(CORPUS_DIR)}
        result = _jobs.enqueue(kind, payload)
        if result is None:
            # duplicate; key_hash already in jobs.sqlite
            continue
        n += 1
    print(f"# enqueue: {n} new jobs added (the rest were duplicates)",
          file=sys.stderr)
    print(f"# to dispatch: `python bin/jobs.py dispatch --limit 50`",
          file=sys.stderr)
    print(f"# to drain continuously: `python bin/jobs.py daemon --interval 20m`",
          file=sys.stderr)
    return 0


def walk(rate: int = 0) -> int:
    """Enqueue + dispatch in one shell, with optional rate-pacing.

    rate=0 (default): no pacing — depends on the worker's
    own backoff. Use rate=5 to sleep 1s every 5 requests.
    """
    rc = enqueue()
    if rc != 0:
        return rc
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import jobs as _jobs  # noqa: E402
    print(f"# walk: dispatching in this shell", file=sys.stderr)
    # dispatch in a loop with optional pacing between calls
    while True:
        ran = _jobs.dispatch_once()
        if ran == 0:
            break
        if rate > 0:
            time.sleep(1.0 / rate)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="print takeout vs analyzed counts")

    en = sub.add_parser("enqueue", help="enqueue not-yet-analyzed URLs")
    en.add_argument("--zip", type=Path, default=None,
                    help="only enqueue URLs from this specific zip")

    wa = sub.add_parser("walk",
                        help="enqueue + dispatch in one shell (no Gemini)")
    wa.add_argument("--rate", type=int, default=0,
                    help="requests per second cap (default: 0 = no cap)")

    args = p.parse_args()
    if args.cmd == "status":
        return status()
    if args.cmd == "enqueue":
        return enqueue(only_zip=args.zip)
    if args.cmd == "walk":
        return walk(rate=args.rate)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
