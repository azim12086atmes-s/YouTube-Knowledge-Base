#!/usr/bin/env python
"""url_source.py — Yield (slug, url, metadata) from a URL source.

Pluggable URL sources for the analyze pipeline. Today only one source is
implemented: `takeout-watch` (Google Takeout JSON watch-history). Future
sources (playlist-url, subscriptions.csv, manual) plug into the same
interface without changing `pipeline.py` or `analyze.py`.

Interface: every source returns an iterable of dicts:
    {"id": "<11-char YT id>", "url": "https://...", "ts": "ISO date", "title": "..."}

After sample/filter, each item is fed to `analyze.py <url>` and a markdown
file is produced in `~/Documents/video-analysis/`.

Usage:
    python url_source.py [--source takeout-watch] [--n 6]
        # default source: takeout-watch, default N: 6
        # auto-picks most-recent JSON-form takeout zip in ~/Downloads

Notes (ponytail):
- Only `takeout-watch` is implemented today. Other sources stay as TODO
  placeholders until a real friction names them.
- Date buckets are `YYYY-MM`; sample picks the last entry per month.
- Within a bucket: deterministic order (timestamps asc).
"""

from __future__ import annotations
import argparse
import json
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse, parse_qs

DOWNLOADS = Path.home() / "Downloads"
WANT_KEY = "watch-history.json"


def slug_from_url(url: str) -> str:
    q = parse_qs(urlparse(url).query)
    return q.get("v", [""])[0]


# ponytail: URL sources as a dispatch dict. Each returns list[dict].
# Add a new source by writing one function + one line here.
def source_takeout_watch(zp: Path) -> list[dict]:
    """Read YouTube watch-history.json from a Takeout zip."""
    out, seen = [], set()
    with zipfile.ZipFile(zp) as zf:
        history_names = [n for n in zf.namelist()
                         if "history" in n and WANT_KEY in n
                         and "YouTube" in n and not n.endswith("/")]
        if not history_names:
            raise SystemExit(
                f"zip {zp.name} has no YouTube watch-history.json — "
                f"only JSON form is supported. Re-takeout the export as JSON."
            )
        for name in history_names:
            with zf.open(name) as f:
                data = json.load(f)
            for row in data:
                url = row.get("titleUrl") or ""
                if "youtube.com/watch" not in url:
                    continue
                vid = slug_from_url(url)
                if len(vid) != 11:
                    continue
                key = (vid, row.get("time", ""))
                if key in seen:
                    continue
                seen.add(key)
                title = row.get("title") or ""
                if isinstance(title, list):
                    title = " ".join(title)
                # ponytail: capture channel from subtitles[0].name; absent on
                # embeds / shorts where uploader is unknown. Cheap to lift now,
                # used later for "channels I watch most" aggregations.
                channel = ""
                subs = row.get("subtitles") or []
                if subs and isinstance(subs, list) and isinstance(subs[0], dict):
                    channel = subs[0].get("name", "")
                out.append({"id": vid, "url": url,
                            "ts": row.get("time", ""),
                            "title": title, "channel": channel})
    return out


# ponytail: same parse as `source_takeout_watch` but yields every URL
# sorted by `ts` ascending. Used for `--resume` mode where the pipeline
# walks the corpus end-to-end in order, not sampled.
def source_takeout_watch_all(zp: Path) -> list[dict]:
    return sorted(source_takeout_watch(zp), key=lambda r: r["ts"])


def pick_takeout_zip(args_path: Path | None) -> Path:
    if args_path:
        if not args_path.is_file():
            raise SystemExit(f"not a file: {args_path}")
        return args_path
    candidates = sorted(DOWNLOADS.glob("takeout-*.zip"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for c in candidates:
        try:
            with zipfile.ZipFile(c) as zf:
                if any(WANT_KEY in n and "YouTube" in n for n in zf.namelist()):
                    return c
        except zipfile.BadZipFile:
            continue
    raise SystemExit(
        f"no JSON-form takeout zip found in {DOWNLOADS}; pass --zip PATH"
    )


def sample(records: list[dict], n: int) -> list[dict]:
    """Pick n entries evenly across `YYYY-MM` buckets."""
    if n <= 0 or len(records) <= n:
        return sorted(records, key=lambda r: r["ts"])

    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        buckets[r["ts"][:7]].append(r)
    for m in buckets:
        buckets[m].sort(key=lambda r: r["ts"])

    months = sorted(buckets.keys())
    if not months:
        return []
    if len(months) >= n:
        idx = sorted({i * (len(months) - 1) // (n - 1) for i in range(n)})
    else:
        idx = list(range(len(months)))
    picks = [buckets[months[i]][-1] for i in idx]
    return sorted(picks, key=lambda r: r["ts"])


def emit(records: list[dict]) -> None:
    # ponytail: 5-column output (channel added). Empty channel = "" so
    # existing 4-column parsers stay compatible via split('|')[:4].
    for r in records:
        title = r["title"].replace("\n", " ").replace("|", "/")[:80]
        channel = r.get("channel", "").replace("|", "/")[:60]
        print(f"{r['id']} | {r['url']} | {r['ts']} | {title} | {channel}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source", default="takeout-watch",
                   help="URL source: takeout-watch (sample, default), "
                        "takeout-watch-all (full chronological scan).")
    # ponytail: positional zip kept for backward compat with pipeline.py / shell aliases.
    p.add_argument("zip_pos", nargs="?", type=Path, default=None,
                   help="(deprecated, prefer --zip)")
    p.add_argument("--zip", type=Path, default=None,
                   help="path to Takeout zip (only used with takeout-watch / takeout-watch-all)")
    p.add_argument("--n", type=int, default=6)
    # ponytail: for takeout-watch-all, --start-index / --limit enable resume.
    # Sample mode (default) ignores these.
    p.add_argument("--start-index", type=int, default=0,
                   help="for takeout-watch-all: cursor position (resume offset)")
    p.add_argument("--limit", type=int, default=0,
                   help="for takeout-watch-all: max records to yield (0 = all remaining)")
    args = p.parse_args()
    args.zip = args.zip or args.zip_pos

    if args.source not in ("takeout-watch", "takeout-watch-all"):
        print(f"unknown --source {args.source!r}; "
              f"only takeout-watch and takeout-watch-all are implemented",
              file=sys.stderr)
        return 2

    zp = pick_takeout_zip(args.zip)

    if args.source == "takeout-watch-all":
        # ponytail: full chronological scan; --start-index and --limit apply.
        records = source_takeout_watch_all(zp)
        if not records:
            print(f"no watch-history entries in {zp.name}", file=sys.stderr)
            return 2
        window = records[args.start_index:]
        if args.limit > 0:
            window = window[:args.limit]
        print(f"# source: {args.source}  file: {zp}", file=sys.stderr)
        print(f"# records: {len(records)} total  cursor: {args.start_index}",
              file=sys.stderr)
        print(f"# yielded: {len(window)} (limit={args.limit or 'none'})",
              file=sys.stderr)
        emit(window)
        return 0

    # takeout-watch (sampled, backward-compatible default)
    records = source_takeout_watch(zp)
    if not records:
        print(f"no watch-history entries in {zp.name}", file=sys.stderr)
        return 2
    picks = sample(records, args.n)
    print(f"# source: {args.source}  file: {zp}", file=sys.stderr)
    print(f"# records: {len(records)} unique watch entries with YouTube URLs",
          file=sys.stderr)
    print(f"# sampled: {len(picks)} (last-entry per month)", file=sys.stderr)
    emit(picks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
