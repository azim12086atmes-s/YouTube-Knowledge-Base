#!/usr/bin/env python
"""list.py — what's in the corpus.

One table summary, no menus. Output columns:
  slug  | url | mode | outcome | tags | analyzed_on

Ponytail: ~50 LOC, stdlib sqlite3 only.

Usage:
  python bin/list.py                       # all rows, default order
  python bin/list.py --tag ai-tooling      # filter by tag (any of, union)
  python bin/list.py --mode multimodal    # only multimodal
  python bin/list.py --outcome skip-junk  # only skips
"""
from __future__ import annotations
import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_INDEX = Path.home() / "Documents" / "video-analysis" / "analyzed.sqlite"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--index", type=Path, default=DEFAULT_INDEX,
                   help=f"path to analyzed.sqlite (default: {DEFAULT_INDEX})")
    p.add_argument("--tag", action="append", default=[],
                   help="filter to slugs having this tag (repeatable; union)")
    p.add_argument("--mode", choices=["transcript", "multimodal"],
                   help="filter by analysis mode")
    p.add_argument("--outcome", help="filter by exact outcome (ok, skip-no-transcript, ...)")
    p.add_argument("--limit", type=int, default=0,
                   help="max rows (0 = all)")
    args = p.parse_args()

    if not args.index.exists():
        print(f"no index at {args.index}; analyze at least one video first",
              file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(args.index))
    where, params = [], []
    if args.mode:
        where.append("v.mode = ?"); params.append(args.mode)
    if args.outcome:
        where.append("v.outcome = ?"); params.append(args.outcome)
    if args.tag:
        placeholders = ",".join("?" for _ in args.tag)
        # ponytail: union semantics — slug has ANY of the requested tags.
        where.append(f"v.slug IN (SELECT slug FROM tag_assignments "
                     f"WHERE tag IN ({placeholders}))")
        params.extend(args.tag)
    sql = "SELECT v.slug, v.url, v.mode, v.outcome, v.analyzed_on " \
          "FROM analyzed_videos v"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY v.analyzed_on DESC, v.slug"
    if args.limit > 0:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql, params).fetchall()

    # ponytail: collect tags per slug in one query, then format in memory.
    # Avoids N+1 roundtrips for big corpora.
    tags_by_slug: dict[str, list[str]] = {}
    if rows:
        slug_placeholders = ",".join("?" for _ in rows)
        tag_rows = conn.execute(
            f"SELECT slug, tag FROM tag_assignments "
            f"WHERE slug IN ({slug_placeholders}) ORDER BY slug, tag",
            [r[0] for r in rows],
        ).fetchall()
        for s, t in tag_rows:
            tags_by_slug.setdefault(s, []).append(t)

    print(f"# {len(rows)} row(s) from {args.index}")
    print("# slug            | mode        | outcome              | tags                                  | analyzed_on")
    for slug, url, mode, outcome, date in rows:
        tags = ",".join(tags_by_slug.get(slug, []))
        print(f"  {slug:14s} | {mode:11s} | {outcome:21s} | "
              f"{tags:38s} | {date}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
