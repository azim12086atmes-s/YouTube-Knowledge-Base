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
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from xml.etree import ElementTree as ET

DOWNLOADS = Path.home() / "Downloads"
WANT_KEY = "watch-history.json"
_XLSX_NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_ID_RE = re.compile(r"(?:v=|youtu\.be/|/)([A-Za-z0-9_-]{11})")


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


# ponytail: xlsx/urlfile sources added when non-Takeout URL files existed in
# ~/Downloads (2026-07-13). Stdlib only — openpyxl is a much heavier install
# for what is, under the hood, a zipfile of XML.
def _xlsx_rows(path: Path) -> list[list[str]]:
    """Yield rows from the first worksheet of an xlsx. Handles sharedStrings
    AND inlineStr. Stdlib only — no openpyxl dep."""
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("s:si", _XLSX_NS):
                shared.append("".join((t.text or "")
                                      for t in si.findall(".//s:t", _XLSX_NS)))
        sheets = sorted(n for n in names
                        if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        if not sheets:
            return []
        ws = ET.fromstring(zf.read(sheets[0]))
        rows: list[list[str]] = []
        for row in ws.findall("s:sheetData/s:row", _XLSX_NS):
            vals: list[str] = []
            for c in row.findall("s:c", _XLSX_NS):
                t = c.attrib.get("t", "n")
                if t == "inlineStr":
                    is_el = c.find("s:is", _XLSX_NS)
                    txt = ""
                    if is_el is not None:
                        txt = "".join((tt.text or "")
                                      for tt in is_el.findall(".//s:t", _XLSX_NS))
                    vals.append(txt)
                    continue
                v = c.find("s:v", _XLSX_NS)
                if v is None or v.text is None:
                    vals.append("")
                elif t == "s":
                    idx = int(v.text)
                    vals.append(shared[idx] if idx < len(shared) else "")
                else:
                    vals.append(v.text)
            rows.append(vals)
        return rows


def _extract_url_ids(*texts: str) -> list[str]:
    """Pull YouTube IDs out of free text — handles watch URLs, youtu.be, bare IDs."""
    ids: list[str] = []
    for t in texts:
        if not t:
            continue
        for m in _ID_RE.finditer(t):
            ids.append(m.group(1))
    return ids


def source_xlsx(path: Path) -> list[dict]:
    """URLs from an Excel file: any cell whose text contains a YouTube id."""
    rows = _xlsx_rows(path)
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        for cell in r:
            if not isinstance(cell, str):
                continue
            for vid in _extract_url_ids(cell):
                if vid in seen:
                    continue
                seen.add(vid)
                out.append({"id": vid,
                            "url": f"https://www.youtube.com/watch?v={vid}",
                            "ts": "", "title": "", "channel": ""})
    return out


# ponytail: plain text/JSONL/JSON URL lists. Heuristic — if line starts with
# '{' parse JSON and look for `url` / `titleUrl` / `link`; else treat as raw
# URL/id. Cheap and most scrapers output one of these shapes.
def source_urlfile(path: Path) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        url = ""
        title = ""
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                url = (obj.get("url") or obj.get("titleUrl")
                       or obj.get("link") or obj.get("video_url") or "")
                title = obj.get("title") or ""
        else:
            url = line
        for vid in _extract_url_ids(url):
            if vid in seen:
                continue
            seen.add(vid)
            out.append({"id": vid,
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "ts": "", "title": title, "channel": ""})
    return out


def pick_xlsx_or_urlfile(path: Path | None) -> Path:
    """If user passed a file: use it. Else auto-discover the first non-empty
    xlsx in DOWNLOADS (the only known URL-list format today)."""
    if path:
        if not path.is_file():
            raise SystemExit(f"not a file: {path}")
        return path
    candidates = sorted(DOWNLOADS.glob("extracted_youtube_urls*.xlsx"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for c in candidates:
        if source_xlsx(c):
            return c
    raise SystemExit(
        f"no non-empty URL-list xlsx in {DOWNLOADS}; pass --file PATH"
    )


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
    """Pick n entries evenly across `YYYY-MM` buckets.

    ponytail: xlsx/urlfile sources have no timestamp → no month buckets →
    fall back to a deterministic `n` strided across the list.
    """
    if n <= 0 or len(records) <= n:
        return sorted(records, key=lambda r: r["ts"])
    # ponytail: if no record carries a usable YYYY-MM prefix, the bucketing
    # collapses to one bucket (last per month == last in list). Detect and
    # stride evenly instead — that's what the caller actually wants when the
    # input has no temporal metadata (xlsx / urlfile sources).
    has_months = any(r["ts"][:7] for r in records)
    if not has_months:
        # ponytail: deterministic stride for chronoless sources; ensure the
        # last element lands by padding with it when stride is coarser than N.
        if not records:
            return []
        step = max(1, len(records) // n)
        out = [records[i] for i in range(0, len(records), step)][:n]
        if len(out) < n:
            out.append(records[-1])
        return out[:n] 

    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        buckets[r["ts"][:7]].append(r)
    for m in buckets:
        buckets[m].sort(key=lambda r: r["ts"])

    months = sorted(buckets.keys())
    if not months:
        return []
    # ponytail: degenerate case — n=1 means "give me one". Avoid div-by-zero
    # in the stride formula; just return the last record of the latest month.
    if n == 1:
        picks = [buckets[months[-1]][-1]]
        return picks
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
                   help="URL source: takeout-watch (sample, default) | "
                        "takeout-watch-all (full chronological scan) | "
                        "xlsx (Excel URL list) | urlfile (txt/jsonl/JSON list)")
    # ponytail: positional zip kept for backward compat with pipeline.py / shell aliases.
    p.add_argument("zip_pos", nargs="?", type=Path, default=None,
                   help="(deprecated, prefer --zip or --file)")
    p.add_argument("--zip", type=Path, default=None,
                   help="path to Takeout zip (only with takeout-watch / "
                        "takeout-watch-all)")
    p.add_argument("--file", type=Path, default=None,
                   help="path to xlsx / txt / jsonl URL file (only with "
                        "xlsx / urlfile)")
    p.add_argument("--n", type=int, default=6)
    # ponytail: for takeout-watch-all, --start-index / --limit enable resume.
    # Sample mode (default) ignores these.
    p.add_argument("--start-index", type=int, default=0,
                   help="for takeout-watch-all: cursor position (resume offset)")
    p.add_argument("--limit", type=int, default=0,
                   help="for takeout-watch-all: max records to yield (0 = all remaining)")
    args = p.parse_args()
    args.zip = args.zip or args.zip_pos

    if args.source not in ("takeout-watch", "takeout-watch-all",
                           "xlsx", "urlfile"):
        print(f"unknown --source {args.source!r}; "
              f"expected takeout-watch | takeout-watch-all | xlsx | urlfile",
              file=sys.stderr)
        return 2

    if args.source in ("xlsx", "urlfile"):
        fp = pick_xlsx_or_urlfile(args.file)
        if args.source == "xlsx":
            records = source_xlsx(fp)
        else:
            records = source_urlfile(fp)
        if not records:
            print(f"no YouTube URLs found in {fp}", file=sys.stderr)
            return 2
        picks = sample(records, args.n) if args.n > 0 else records
        print(f"# source: {args.source}  file: {fp}", file=sys.stderr)
        print(f"# records: {len(records)}  sampled: {len(picks)}", file=sys.stderr)
        emit(picks)
        return 0

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
