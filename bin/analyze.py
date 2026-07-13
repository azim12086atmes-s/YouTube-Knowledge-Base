#!/usr/bin/env python
"""analyze.py — One URL in, one markdown file out.

What it does:
1. POSTs the four-shape A/B prompts (summary / takeaways / insights / reframes) to
   Gemini 3.1-flash-lite with the YouTube URL as `fileData`.
2. Retries 429 (quota) with exponential backoff up to N calls (default 6 = free-tier budget).
3. Falls back to transcript-only path via the youtube-content skill helper if
   multimodal returns 403 PERMISSION_DENIED or finishes with at least one ERROR.
4. Writes the markdown file matching ~/Documents/video-analysis/CONVENTIONS.md.
5. Skip-write + stderr message if signal is insufficient
   (e.g. transcripts disabled AND multimodal failed).

Dependency: google-genai in the active Python env. Stdlib otherwise.

Usage:
    python analyze.py https://www.youtube.com/watch?v=<id>
    python analyze.py <id>
    python analyze.py URL1 URL2 URL3 ...

Output folder (override with $VIDEO_ANALYSIS_DIR):
    C:/Users/karee/Documents/video-analysis/

What's NOT in this script (rung-1 avoided):
- Takeout zip extraction + sample-picking (separate `takeout_sample.py`).
- Classification / tagging (will be hand-done until 10+ files; see REQUIREMENTS.md D3).
- Multi-threaded concurrent Gemini calls (rung-2; sequential with backoff is simpler).
- Custom prompts (the 4-shape convention from CONVENTIONS.md is the only output shape).
"""

from __future__ import annotations
import argparse
import base64
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse as _urlparse  # ponytail: used only by --retry-skips
from _gemini import (gemini_key as _gemini_key,
                     post_text as _gemini_post_text,
                     post as _gemini_post)

# ponytail: back-compat shim. Real helper lives in _gemini.py.
gemini_key = _gemini_key


# ponytail: v1 stays inline. If conventions change, lift these to a shared module.
PROMPTS_MULTIMODAL = {
    "1_summary": """Summarize this video for someone who hasn't watched it.
Format:
- One-paragraph gist (3-5 sentences)
- Main argument in 2 sentences
- 4-6 subtopics with 1-sentence description + timestamp (mm:ss)""",

    "2_key_takeaways": """Extract 7-10 KEY TAKEAWAYS from this video.
Each:
- Bold headline
- 2-3 sentences explaining concretely
- Timestamps (mm:ss) where stated
- "Why it matters" line""",

    "3_non_obvious_insights": """Extract 5-8 NON-OBVIOUS INSIGHTS from this video.
Each:
- Sharp 1-sentence insight
- Why non-obvious (what people miss)
- Timestamps (mm:ss)
- "Extends to" line: where this insight applies beyond the video""",

    "4_revolutionary_reframes": """Find 3-5 REVOLUTIONARY REFRAMES in this video — ideas that challenge conventional assumptions about software, AI, work, or running a business.
Each:
- Reframe in 1 sharp sentence
- Old assumption replaced
- Timestamps (mm:ss)
- Skeptical pushback: where this might be wrong (be honest)
""",
}

PROMPTS_TRANSCRIPT = {
    "1_summary": """You will be given the YouTube transcript of a video. You CANNOT see the video. Reason ONLY from the transcript text.

Summarize the video for someone who hasn't read it.
Format:
- One-paragraph gist (3-5 sentences)
- Main argument in 2 sentences
- 4-6 subtopics each with a 1-sentence description + the timestamp (mm:ss) where each begins in the transcript

If the transcript is sparse (e.g. only [Music] lines, or fewer than 20 lines of speech), say so in the gist and skip the subtopics list.""",

    "2_key_takeaways": """You will be given the YouTube transcript of a video. You CANNOT see the video. Reason ONLY from the transcript.

Extract 5-10 KEY TAKEAWAYS from the transcript.
Each:
- Bold headline
- 2-3 sentences explaining it concretely
- Timestamps (mm:ss) where the speaker stated or demonstrated it (cite multiple timestamps if applicable)
- "Why it matters" line in plain language

Prioritize concrete claims, named tools/people, numbers, and actionable advice over generic platitudes. If the transcript is low-signal (ads, music lyrics, single repeated phrase), say so explicitly and return fewer takeaways.""",

    "3_non_obvious_insights": """You will be given the YouTube transcript of a video. You CANNOT see the video. Reason ONLY from the transcript.

Extract 3-7 NON-OBVIOUS INSIGHTS from the transcript — insights a reader would screenshot and save.
Each:
- The insight in one sharp sentence
- Why it's non-obvious (what most people miss)
- Timestamps (mm:ss) where it's stated or strongly implied
- A short "extends to" line: where this insight applies beyond the video

Skip insights that are generic platitudes. Only insights actually supported by the transcript.""",

    "4_revolutionary_reframes": """You will be given the YouTube transcript of a video. You CANNOT see the video. Reason ONLY from the transcript.

Find 1-4 REVOLUTIONARY REFRAMES in the video — ideas that CHALLENGE conventional assumptions about software, AI, work, or running a business. Don't list things that are merely "good advice."
Each:
- The reframe in one sharp sentence
- The old assumption it replaces
- Timestamps (mm:ss) where it's stated
- A skeptical-pushback line: where this argument might be WRONG or overclaimed

Be honest if the transcript carries fewer than 1 such reframe. If you find 4, list 4. If only 1, list 1. Do not pad.""",
}

GEMINI_TEXT_MODEL = "gemini-flash-lite-latest"
GEMINI_MULTI_MODEL = "gemini-3.1-flash-lite"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"

DEFAULT_OUT = Path.home() / "Documents" / "video-analysis"
# ponytail: index of analyzed videos. Single SQLite table; stdlib only.
# Tracks slug + when + mode + outcome. Used for dedup against revisit
# and as a queryable index when the corpus grows past 50 files.
INDEX_DB_PATH = DEFAULT_OUT / "analyzed.sqlite"
SKILL_HELPER = Path.home() / "AppData" / "Local" / "hermes" / "skills" / "media" / "youtube-content" / "scripts" / "fetch_transcript.py"


def init_index() -> "sqlite3.Connection":
    """Lazy-create the analyzed_videos table. Returns a connection."""
    import sqlite3  # stdlib; ponytail: local import keeps top-of-file lean.
    INDEX_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(INDEX_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analyzed_videos (
            slug        TEXT PRIMARY KEY,
            url         TEXT NOT NULL,
            analyzed_on TEXT NOT NULL,
            mode        TEXT NOT NULL,
            outcome     TEXT NOT NULL,
            out_path    TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def already_analyzed(conn: "sqlite3.Connection", slug: str) -> bool:
    """True if `slug` is in the index with a written file (any mode)."""
    row = conn.execute(
        "SELECT out_path FROM analyzed_videos WHERE slug = ?", (slug,)
    ).fetchone()
    if not row:
        return False
    return Path(row[0]).exists()


def record_analyzed(conn: "sqlite3.Connection", slug: str, url: str,
                    mode: str, outcome: str, out_path: Path) -> None:
    """Upsert a row. Idempotent — re-analyzing a video updates the row."""
    conn.execute("""
        INSERT INTO analyzed_videos (slug, url, analyzed_on, mode, outcome, out_path)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            url = excluded.url,
            analyzed_on = excluded.analyzed_on,
            mode = excluded.mode,
            outcome = excluded.outcome,
            out_path = excluded.out_path
    """, (slug, url, time.strftime("%Y-%m-%d"), mode, outcome, str(out_path)))
    conn.commit()


# ponytail: shim for callers importing this module.
def gemini_key_local():
    return _gemini_key()


def _post(model: str, api_key: str, body: dict, timeout: int = 180) -> str:
    # ponytail: thin wrapper — real POST lives in _gemini.py. Keeps the
    # local _post() signature so internal callers don't need changes.
    contents = body.get("contents", [])
    out = _gemini_post(contents, api_key, model, timeout=timeout)
    if out == "(empty response)":
        return "ERROR EMPTY_PARTS"
    return out

def call_gemini_multimodal(api_key: str, video_url: str, prompt: str,
                            max_retries: int = 3, base_delay: float = 4.0) -> str:
    """POST one prompt to Gemini multimodal. Returns text or 'ERROR <code>: ...'."""
    body = {
        "contents": [{"role": "user", "parts": [
            {"text": prompt},
            {"fileData": {"fileUri": video_url, "mimeType": "video/mp4"}},
        ]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 8192},
    }
    for attempt in range(max_retries):
        result = _post(GEMINI_MULTI_MODEL, api_key, body)
        if not result.startswith("ERROR 429"):
            return result
        if attempt < max_retries - 1:
            # ponytail: free-tier quota — back off aggressively.
            wait = base_delay * (2 ** attempt)
            print(f"  429 quota; sleeping {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
    return result  # last error


def call_gemini_text(api_key: str, prompt: str, transcript: str,
                     max_retries: int = 3, base_delay: float = 2.0) -> str:
    """POST one prompt to Gemini text-only. Transcript is supplied inline.

    Transcripts can be ~30-50 KB. We truncate to 60 KB to stay below input limits
    and avoid surprises. If a future transcript exceeds that bound, switch to a
    chunked approach.
    """
    truncated = transcript if len(transcript) <= 60_000 else transcript[:60_000] + "\n\n[transcript truncated at 60 KB]"
    body = {
        "contents": [{"role": "user", "parts": [
            {"text": f"{prompt}\n\n---\n\nTranscript:\n```\n{truncated}\n```"},
        ]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 8192},
    }
    for attempt in range(max_retries):
        result = _post(GEMINI_TEXT_MODEL, api_key, body, timeout=120)
        if not result.startswith("ERROR 429"):
            return result
        if attempt < max_retries - 1:
            wait = base_delay * (2 ** attempt)
            print(f"  429 quota; sleeping {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
    return result  # last error


# ponytail: classify any text into the fixed vocabulary. Parameterized so
# callers can pass either a transcript or the markdown analysis body.
# Cheap (one short Gemini call, ~1s wall).
def classify_text(api_key: str, text: str,
                  vocab: tuple[str, ...],
                  kind: str = "transcript") -> list[str]:
    """kind ∈ {"transcript", "analysis-body"} — adjusts the prompt framing."""
    vocab_str = ", ".join(f"\"{t}\"" for t in vocab)
    if kind == "transcript":
        src_intro = "You classify YouTube transcripts. The transcript is below."
        src_label = "Transcript"
    else:
        src_intro = ("You classify YouTube videos based on the analysis below. "
                     "The analysis was produced by another model from the video; "
                     "reason about it as if summarizing a watched video.")
        src_label = "Analysis"
    prompt = (
        f"{src_intro}\n\n"
        f"Pick 1-3 tags from this fixed vocabulary that describe the topic(s):\n"
        f"{vocab_str}\n\n"
        f"Output ONLY a JSON array of strings. Nothing else. Example: [\"ai-tooling\"]\n\n"
        f"{src_label}:\n```\n{text[:20_000]}\n```"
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 64},
    }
    result = _post(GEMINI_TEXT_MODEL, api_key, body, timeout=60)
    if result.startswith("ERROR"):
        return []
    import re
    m = re.search(r"\[([^\[\]]*)\]", result)
    if not m:
        return []
    raw = m.group(1)
    parts = [p.strip().strip('"').strip("'") for p in raw.split(",")]
    valid = set(vocab)
    seen = set()
    out = []
    for p in parts:
        if not p or p in seen or p not in valid:
            continue
        seen.add(p)
        out.append(p)
    return out


# ponytail: back-compat alias used by --reclassify. classify_text() with the
# default kind="transcript" is identical to the old classify_transcript().
def classify_transcript(api_key: str, transcript: str,
                        vocab: tuple[str, ...]) -> list[str]:
    return classify_text(api_key, transcript, vocab)



def fetch_transcript(video_url: str) -> Optional[str]:
    """Call the youtube-content skill helper. Returns text on success, None on failure.

    ponytail: the helper writes a JSON error envelope on stdout when YouTube
    refuses the transcript. Distinguish that from a real transcript by sniffing
    the leading character (`{`). rc=0 alone isn't enough — error paths return rc=0.
    """
    if not SKILL_HELPER.exists():
        return None
    try:
        result = subprocess.run(
            [sys.executable, str(SKILL_HELPER), video_url, "--text-only"],
            capture_output=True, text=True, timeout=30,
        )
        out = result.stdout.strip()
        if not out or out.startswith("{"):
            return None
        return out
    except Exception:
        return None


# ponytail: junk-caption threshold — <200 chars OR >50% non-speech content.
# Tuned against the dJWFUBAUM0E case (75% [Music] loop) and wgOOBW3CJIY
# (single-line transcript with trailing [Music] that we still want to analyze).
_NON_SPEECH = ("[music]", "[applause]", "[laughter]", "[inaudible]",
               "[__]", "[inaud.]", "(...)")


def _line_is_non_speech(line: str) -> bool:
    """True if a line is dominated by non-speech tokens, not just contains one."""
    lower = line.lower()
    has_marker = any(tok in lower for tok in _NON_SPEECH)
    if not has_marker:
        return False
    # ponytail: count words. Line is non-speech if >50% of its word tokens
    # are within [brackets] (the canonical non-speech enclosure).
    words = [w.strip(".,!?") for w in lower.split() if w.strip(".,!?")]
    bracketed = sum(1 for w in words if w.startswith("[") or w.startswith("("))
    return words and (bracketed / len(words)) > 0.5


def _transcript_has_signal(transcript: str) -> bool:
    """True if the transcript has enough speech content to be worth analyzing."""
    text = transcript.strip()
    if len(text) < 200:
        return False
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    non_speech = sum(1 for l in lines if _line_is_non_speech(l))
    return (non_speech / len(lines)) <= 0.5


def slug_from_url(url_or_id: str) -> str:
    """Pull the 11-char YouTube ID from a URL or a bare ID."""
    m = re.search(r"(?:v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})", url_or_id)
    if m:
        return m.group(1)
    if re.match(r"^[A-Za-z0-9_-]{11}$", url_or_id):
        return url_or_id
    raise SystemExit(f"could not parse YouTube ID from: {url_or_id!r}")


def full_url(url_or_id: str) -> str:
    slug = slug_from_url(url_or_id)
    return f"https://www.youtube.com/watch?v={slug}"


def _write_tags_to_frontmatter(md_path: Path, tags: list[str]) -> None:
    """Replace the `tags:` line in the front-matter with the new tag list.
    Idempotent. Creates the file with a stub front-matter if missing."""
    if not md_path.exists():
        return
    import re as _re
    text = md_path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            head, rest = text[:end + 4], text[end + 4:]
        else:
            head, rest = text, ""
    else:
        head, rest = "", "\n" + text
    rendered = "[" + ", ".join(tags) + "]" if tags else "[]"
    if _re.search(r"^tags:\s*\[", head, _re.M):
        head = _re.sub(r"^tags:\s*\[.*?\]", f"tags: {rendered}", head,
                       count=1, flags=_re.M)
    else:
        head = head.rstrip()
        if head.endswith("---"):
            head = head[:-3].rstrip() + f"\ntags: {rendered}\n---"
        else:
            head = head + f"\ntags: {rendered}"
    md_path.write_text(head + rest, encoding="utf-8")


def write_markdown(out_path: Path, slug: str, url: str, watched_at: str,
                   pkg: dict, mode: str, errors: list) -> None:
    """Match CONVENTIONS.md front-matter + 4-shape body.

    mode ∈ {"multimodal", "transcript"} — controls only the front-matter `analyzed_with:` line.
    """
    if out_path.exists():
        print(f"  skip {slug}: {out_path.name} already exists", file=sys.stderr)
        return

    today = time.strftime("%Y-%m-%d")
    if mode == "transcript":
        analyzed_with = "gemini-flash-lite-latest (transcript-only)"
    else:
        analyzed_with = "gemini-3.1-flash-lite (multimodal)"

    fm = (
        "---\n"
        f"youtube_id: {slug}\n"
        f"url: {url}\n"
        f"title: \"(see URL)\"\n"
        f"analyzed_on: {today}\n"
        f"analyzed_with: {analyzed_with}\n"
        f"watched_at: {watched_at or 'unknown'}\n"
        "duration: unknown\n"
        "tags: []\n"
        "---\n\n"
    )
    body = f"# {slug}\n\n"
    for shape, header in [
        ("1_summary", "## 1. Summary"),
        ("2_key_takeaways", "## 2. Key Takeaways"),
        ("3_non_obvious_insights", "## 3. Non-Obvious Insights"),
        ("4_revolutionary_reframes", "## 4. Revolutionary Reframes"),
    ]:
        body += f"{header}\n\n{pkg.get(shape, '(unavailable)').strip()}\n\n"

    err_note = ""
    if errors:
        err_note = "\n## Errors observed\n"
        for shape, err in errors:
            err_note += f"- `{shape}`: {err}\n"

    body += (
        "## Provenance & notes\n"
        f"- Source: `analyze.py {url}` (mode: {mode})\n"
        f"- LLM model: {analyzed_with}.\n"
        f"{err_note}"
    )
    out_path.write_text(fm + body, encoding="utf-8")
    print(f"  wrote {out_path}")


def process_one(api_key: str, slug: str, watched_at: str, out_dir: Path,
                 mode: str, index_conn=None) -> int:
    """Returns 0 on success, 1 on skip (insufficient signal).

    mode ∈ {"multimodal", "transcript"}.
    Transcript mode fetches the YouTube transcript first, then POSTs 4 text-only prompts
    to gemini-flash-lite-latest. If the transcript is unavailable and we're in
    transcript-only mode, skip the write entirely (don't fall back to multimodal —
    it would silently change mode). The flag is the user's explicit choice.

    ponytail: index_conn, when supplied, is consulted for prior analysis (dedup)
    and updated on every terminal outcome.
    """
    url = full_url(slug)
    out_path = out_dir / f"{slug}.md"

    # ponytail: dedup. If the index says we've analyzed this slug and the
    # markdown file still exists, short-circuit.
    if index_conn is not None and already_analyzed(index_conn, slug):
        print(f"  SKIP {slug}: already analyzed (index hit; see analyzed.sqlite)",
              file=sys.stderr)
        return 0

    print(f"\n=== {slug} ===")
    print(f"  url: {url}")
    print(f"  mode: {mode}")

    prompts = PROMPTS_MULTIMODAL if mode == "multimodal" else PROMPTS_TRANSCRIPT
    pkg = {}
    errors = []

    if mode == "transcript":
        # ponytail: front-load the only failure mode that doesn't burn quota.
        transcript = fetch_transcript(url)
        if not transcript:
            print(f"  SKIP {slug}: transcript unavailable or disabled by uploader", file=sys.stderr)
            if index_conn is not None:
                record_analyzed(index_conn, slug, url, mode, "skip-no-transcript", out_path)
            return 1
        if not _transcript_has_signal(transcript):
            print(f"  SKIP {slug}: transcript is junk (< 200 chars of speech, or >50% non-speech lines)", file=sys.stderr)
            if index_conn is not None:
                record_analyzed(index_conn, slug, url, mode, "skip-junk", out_path)
            return 1
        # Save the transcript for re-use.
        (out_dir / f"{slug}.transcript.txt").write_text(transcript, encoding="utf-8")
        # ponytail: embed for semantic search on the same SQLite connection.
        # ~50 ms on CPU; idempotent on re-runs (upsert_chunks deletes prior).
        if index_conn is not None:
            try:
                from vector_store import upsert_chunks
                n = upsert_chunks(index_conn, slug, transcript)
                print(f"  embed: {n} chunks stored", file=sys.stderr)
            except Exception as e:
                print(f"  embed: skipped ({type(e).__name__}: {e})", file=sys.stderr)

    for shape, prompt in prompts.items():
        if mode == "multimodal":
            result = call_gemini_multimodal(api_key, url, prompt)
        else:
            # transcript mode — re-read from disk so we don't lose data in errors.
            tpath = out_dir / f"{slug}.transcript.txt"
            transcript = tpath.read_text(encoding="utf-8") if tpath.exists() else ""
            result = call_gemini_text(api_key, prompt, transcript)
        if result.startswith("ERROR"):
            errors.append((shape, result))
            pkg[shape] = f"({mode} shape unavailable)"
            print(f"  {shape}: {result[:80]}")
        else:
            pkg[shape] = result
            chars = len(result)
            print(f"  {shape}: OK ({chars} chars)")
        # light spacing to stay under free-tier rate
        time.sleep(2)

    # Skip-write if everything errored (no point in producing a stub).
    if len(pkg) > 0 and len(errors) < len(pkg):
        write_markdown(out_path, slug, url, watched_at, pkg, mode, errors)
        if index_conn is not None:
            outcome = "ok-with-errors" if errors else "ok"
            record_analyzed(index_conn, slug, url, mode, outcome, out_path)
        return 0
    print(f"  SKIP {slug}: all {len(errors)} {mode} shapes errored", file=sys.stderr)
    if index_conn is not None:
        record_analyzed(index_conn, slug, url, mode, "skip-all-errored", out_path)
    return 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("urls", nargs="*", help="YouTube URLs or 11-char video IDs")
    p.add_argument("--watched-at", default="",
                   help="ISO timestamp of when the user watched this video "
                        "(from Takeout's `time` field). Lifted into front-matter; "
                        "empty means 'unknown'.")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                   help=f"Output directory (default: {DEFAULT_OUT})")
    p.add_argument("--transcript", dest="transcript", action="store_true", default=True,
                   help="default; transcript-only mode (text LLM, ~3s/video, free tier friendly)")
    p.add_argument("--multimodal", dest="transcript", action="store_false",
                   help="opt-in to Gemini multimodal mode (Gemini watches the video; ~11s/video, free-tier quota risk)")
    p.add_argument("--reindex-from-md", action="store_true",
                   help="backfill the SQLite index from existing markdown files in --out; "
                        "one-time data-quality fix when the index is sparse")
    p.add_argument("--classify", action="store_true",
                   help="after the 4-shape analysis, run a small Gemini call to "
                        "classify the transcript into TAG_VOCAB; writes tags to front-matter + tag_assignments table")
    p.add_argument("--reclassify", action="store_true",
                   help="iterate every markdown file in --out and (re-)classify it "
                        "without re-running the 4-shape analysis; updates front-matter + tag_assignments")
    p.add_argument("--reclassify-from-md", action="store_true",
                   help="same as --reclassify but classifies from the markdown body "
                        "(Summary + Key Takeaways sections) instead of the transcript "
                        "sidecar. Use this for multimodal-mode files that have no "
                        ".transcript.txt. Skips any file that already has tags unless "
                        "--reclassify is also passed (then forces overwrite).")
    p.add_argument("--retry-skips", action="store_true",
                   help="iterate every slug in the index whose outcome starts with "
                        "'skip' and re-run process_one() on it. Useful because "
                        "uploader captions sometimes re-appear days after the "
                        "first try. Idempotent: when an analyze write happens "
                        "the row updates; when no signal surfaces, it stays.")
    args = p.parse_args()

    mode = "transcript" if args.transcript else "multimodal"
    print(f"# mode: {mode}", file=sys.stderr)

    api_key = gemini_key()
    args.out.mkdir(parents=True, exist_ok=True)

    # ponytail: single connection for the whole run. Closes at end of main().
    index_conn = init_index()

    if getattr(args, "reindex_from_md", False):
        # ponytail: backfill the SQLite index from existing markdown files.
        # One-time data-quality fix. Reads front-matter, upserts row.
        import re as _re
        slug_re = _re.compile(r"^([A-Za-z0-9_-]{11})\.md$")
        files = sorted(p for p in args.out.glob("*.md")
                       if slug_re.match(p.name))
        print(f"# reindex: scanning {len(files)} candidate files in {args.out}",
              file=sys.stderr)
        updated = 0
        for p in files:
            slug = slug_re.match(p.name).group(1)
            text = p.read_text(encoding="utf-8")
            # ponytail: parse front-matter with regex. CONVENTIONS.md names
            # the canonical field `youtube_id` (not `slug`).
            slug_m = _re.search(r"^youtube_id:\s*([A-Za-z0-9_-]+)\s*$", text, _re.M)
            url_m = _re.search(r"^url:\s*(\S+)\s*$", text, _re.M)
            mode_m = _re.search(r"^analyzed_with:\s*(.+?)\s*$", text, _re.M)
            date_m = _re.search(r"^analyzed_on:\s*(\S+)\s*$", text, _re.M)
            # ponytail: skip meta-docs that look like 11-char slugs but aren't.
            # Files like CONVENTIONS.md / REQUIREMENTS.md match the filename regex
            # but have no `youtube_id:` matching the filename.
            if not slug_m or slug_m.group(1) != slug:
                print(f"  skip meta-doc: {p.name} (slug mismatch)", file=sys.stderr)
                continue
            record_analyzed(index_conn,
                            slug_m.group(1),
                            url_m.group(1) if url_m else f"https://www.youtube.com/watch?v={slug}",
                            "transcript" if (mode_m and "transcript" in mode_m.group(1).lower())
                                       else "multimodal",
                            "ok", p)
            # ponytail: index via the body-text helper so multimodal-mode files
            # (no transcript sidecar) still get embedded. Text source is the
            # transcript sidecar when present, otherwise ## 1. Summary + ## 2.
            # Key Takeaways from the markdown body; upsert_chunks() is the
            # single embedding path for both cases.
            from vector_store import body_text_for_indexing, upsert_chunks
            text = body_text_for_indexing(p)
            if text.strip():
                try:
                    n = upsert_chunks(index_conn, slug_m.group(1), text)
                    src = "transcript" if (args.out / f"{slug_m.group(1)}.transcript.txt").exists() else "analysis-body"
                    print(f"  embed: {slug_m.group(1)} -> {n} chunks [{src}]",
                          file=sys.stderr)
                except Exception as e:
                    print(f"  embed: skipped {slug_m.group(1)} "
                          f"({type(e).__name__}: {e})", file=sys.stderr)
            else:
                print(f"  embed: skipped {slug_m.group(1)} (no usable text)",
                      file=sys.stderr)
            updated += 1
        # ponytail: remove prior bogus rows. Real YT IDs are mixed-case +
        # alphanumeric; pure-uppercase / pure-lower is unusual. Cheap filter.
        index_conn.execute(
            "DELETE FROM analyzed_videos WHERE slug = slug AND "
            "(slug = UPPER(slug) OR slug = LOWER(slug)) AND "
            "slug NOT GLOB '*[0-9]*'"
        )
        index_conn.commit()
        index_conn.close()
        print(f"# reindex: upserted {updated} rows into {INDEX_DB_PATH}",
              file=sys.stderr)
        return 0

    # ponytail: --reclassify iterates every markdown file in --out that has
    # a transcript sidecar, classifies via Gemini, and writes tags back to
    # front-matter + tag_assignments. No re-running of the 4-shape analysis.
    if getattr(args, "reclassify", False) or getattr(args, "reclassify_from_md", False):
        from vector_store import set_tags as _set_tags
        from vector_store import get_tags as _get_tags
        import re as _re
        from vector_store import TAG_VOCAB as _VOCAB
        slug_re = _re.compile(r"^([A-Za-z0-9_-]{11})\.md$")
        files = sorted(p for p in args.out.glob("*.md")
                       if slug_re.match(p.name))
        print(f"# reclassify: {len(files)} candidate files", file=sys.stderr)
        force = getattr(args, "reclassify", False)
        use_body = getattr(args, "reclassify_from_md", False) and not force
        for p in files:
            slug = slug_re.match(p.name).group(1)
            text = ""
            kind = "transcript"
            if use_body:
                # ponytail: multimodal-mode files have no transcript sidecar.
                # Read Summary + Key Takeaways from the markdown body instead.
                full = p.read_text(encoding="utf-8")
                # Take the prose between "## 1. Summary" and "## 2. Key Takeaways"
                # (and before "## 3."), max ~15k chars per section.
                m1 = _re.search(r"##\s*1\.\s*Summary\s*\n(.*?)(?=\n##\s|\Z)",
                                full, _re.S)
                m2 = _re.search(r"##\s*2\.\s*Key Takeaways\s*\n(.*?)(?=\n##\s|\Z)",
                                full, _re.S)
                if m1: text += m1.group(1).strip()[:15_000]
                if m2: text += "\n\n" + m2.group(1).strip()[:15_000]
                kind = "analysis-body"
                if not text.strip():
                    print(f"  skip {slug}: empty analysis body", file=sys.stderr)
                    continue
            else:
                tpath = args.out / f"{slug}.transcript.txt"
                if not tpath.exists():
                    print(f"  skip {slug}: no transcript sidecar", file=sys.stderr)
                    continue
                text = tpath.read_text(encoding="utf-8")
            # ponytail: avoid burning a Gemini call when tags already exist
            # AND we're in --reclassify-from-md (idempotent on already-tagged).
            existing = _get_tags(index_conn, slug)
            if existing and not force:
                print(f"  skip {slug}: already tagged {existing}", file=sys.stderr)
                continue
            tags = classify_text(api_key, text, _VOCAB, kind=kind)
            _set_tags(index_conn, slug, tags)
            _write_tags_to_frontmatter(p, tags)
            print(f"  {slug}: {','.join(tags) if tags else '(no tags)'} "
                  f"[from {kind}]", file=sys.stderr)
        index_conn.close()
        return 0

    # ponytail: --retry-skips iterates the index, finds every row whose outcome
    # starts with 'skip', re-runs process_one(). A skip row's *file* is the
    # absent <slug>.md; analyze.py's dedup returns 0 only when the *file* exists
    # so a re-run is a real "did captions appear?" probe, not a no-op.
    if getattr(args, "retry_skips", False):
        from urllib.parse import urlparse
        skip_slugs = [r[0] for r in index_conn.execute(
            "SELECT slug FROM analyzed_videos WHERE outcome LIKE 'skip%' "
            "ORDER BY slug"
        )]
        print(f"# retry-skips: {len(skip_slugs)} slug(s)", file=sys.stderr)
        recovered = 0
        for slug in skip_slugs:
            # ponytail: pull the URL back out of the index so the user doesn't
            # have to remember which skip came from which takeout.
            row = index_conn.execute(
                "SELECT url FROM analyzed_videos WHERE slug = ?", (slug,)
            ).fetchone()
            if not row:
                continue
            url = row[0]
            print(f"  retry {slug}  ({url})", file=sys.stderr)
            # ponytail: --watched-at is per-invocation (CLI), so a
            # --retry-skips batch doesn't try to be smart about per-slug
            # timestamps. Re-tries keep the original 'unknown' front-matter.
            r = process_one(api_key, slug, "", args.out, mode, index_conn)
            if r == 0:
                recovered += 1
        print(f"# retry-skips: {recovered}/{len(skip_slugs)} recovered",
              file=sys.stderr)
        index_conn.close()
        return 0 if recovered else 1

    skipped = 0
    try:
        for u in args.urls:
            slug = slug_from_url(u)
            result = process_one(api_key, slug, args.watched_at, args.out,
                                 mode, index_conn)
            if result == 1:
                skipped += 1
                continue
            # ponytail: --classify adds one cheap Gemini call (~1s, 64 output
            # tokens) per successful analyze. Works on transcript AND
            # multimodal-mode files — body_text_for_classify() picks the
            # best-available text. Skipped silently on classification failure
            # or when the slug has no usable text.
            if getattr(args, "classify", False):
                from vector_store import (set_tags as _set_tags, TAG_VOCAB as _VOCAB,
                                         body_text_for_classify as _btc)
                text, kind = _btc(slug, args.out)
                if not kind:
                    print(f"  classify: skipped {slug} (no transcript or body)",
                          file=sys.stderr)
                    continue
                tags = classify_text(api_key, text, _VOCAB, kind=kind)
                _set_tags(index_conn, slug, tags)
                _write_tags_to_frontmatter(args.out / f"{slug}.md", tags)
                print(f"  classify: {','.join(tags) if tags else '(no tags)'} "
                      f"[from {kind}]", file=sys.stderr)
    finally:
        index_conn.close()

    print(f"\ndone: {len(args.urls) - skipped} written, {skipped} skipped")
    return 0 if skipped == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
