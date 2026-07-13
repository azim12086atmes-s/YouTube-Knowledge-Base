#!/usr/bin/env python
"""backfill_watched_at.py — populate `watched_at:` in markdown front-matter
from a Takeout zip's watch-history.

ponytail: when the corpus has 'watched_at: unknown' front-matter but the
underlying Takeout knew exactly when the video was watched, this lifts
that timestamp back into the file. ONE arg: --zip PATH. Re-runnable; only
touches files where the front-matter currently says 'unknown' (so it never
overrides a more-specific value someone wrote by hand).

Stdlib only. No LLM calls.
"""
from __future__ import annotations
import argparse
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlparse, parse_qs


CORPUS = Path.home() / "Documents" / "video-analysis"
WANT_KEY = "watch-history.json"
FRONT_RE = re.compile(r"^watched_at:\s*(.*)$", re.M)


def slug_from_url(url: str) -> str:
    return parse_qs(urlparse(url).query).get("v", [""])[0]


def watched_index_from_zip(zp: Path) -> dict[str, list[str]]:
    """slug -> [iso_timestamp, ...] from the zip's watch-history."""
    out: dict[str, list[str]] = {}
    with zipfile.ZipFile(zp) as zf:
        names = [n for n in zf.namelist()
                 if WANT_KEY in n and "YouTube" in n and not n.endswith("/")]
        for name in names:
            import json
            data = json.loads(zf.read(name))
            for row in data:
                url = row.get("titleUrl") or ""
                if "youtube.com/watch" not in url:
                    continue
                s = slug_from_url(url)
                if len(s) != 11:
                    continue
                ts = row.get("time", "")
                if ts:
                    out.setdefault(s, []).append(ts)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--zip", type=Path, required=True,
                   help="path to Takeout zip with watch-history.json")
    p.add_argument("--corpus", type=Path, default=CORPUS,
                   help=f"corpus dir (default: {CORPUS})")
    args = p.parse_args()

    if not args.zip.exists():
        print(f"not a file: {args.zip}", file=sys.stderr); return 2
    if not args.corpus.exists():
        print(f"no corpus at {args.corpus}", file=sys.stderr); return 2

    watch = watched_index_from_zip(args.zip)
    print(f"# zip: {args.zip.name}")
    print(f"# unique slugs in watch-history: {len(watch)}", file=sys.stderr)

    # ponytail: latest watch-time per slug (people re-watch; keep most recent).
    latest = {s: max(ts) for s, ts in watch.items() if ts}

    updated = 0
    skipped_unknown = 0
    skipped_missing = 0
    for md in sorted(args.corpus.glob("*.md")):
        text = md.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end < 0:
            continue
        m = FRONT_RE.search(text[:end])
        if not m:
            continue
        # Skip files where watched_at is empty (let user fill in by hand)
        cur = m.group(1).strip()
        if cur and cur != "unknown":
            continue
        # Extract slug from filename or youtube_id line.
        slug_m = re.search(r"^youtube_id:\s*([A-Za-z0-9_-]+)\s*$",
                           text[:end], re.M)
        if not slug_m:
            continue
        slug = slug_m.group(1)
        if slug not in latest:
            skipped_missing += 1
            continue
        ts = latest[slug]
        new = FRONT_RE.sub(f"watched_at: {ts}", text[:end], count=1) + text[end:]
        md.write_text(new, encoding="utf-8")
        updated += 1
        print(f"  {slug}: {cur!r} -> {ts!r}")

    print(f"\n# updated {updated}; skipped {skipped_missing} not in takeout",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
