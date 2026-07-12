# REQUIREMENTS — Video Analysis Pipeline

**Status:** draft, opened 2026-07-10. Next refresh after first 5 video analyses land.

## What we promised today (rung-7 already shipped)

1. One YouTube URL produces one markdown file in `~/Documents/video-analysis/` with the 4-shape A/B convention (Summary / Key Takeaways / Non-Obvious Insights / Revolutionary Reframes). ✓ `M1E4ZzdpOco.md` exists.
2. Format = YAML front-matter + 4 markdown sections + Provenance. Spec in `CONVENTIONS.md`. ✓.
3. Folder lives in `~/Documents/` so Obsidian auto-surfaces it; no plugin needed.

## What we deferred (rung-1 to rung-5, depends on a friction)

These are **explicitly NOT built today**. Each has a trigger — do not implement until the trigger fires.

| # | Deferred work | Trigger to start |
|---|---|---|
| D1 | **Open Notebook ingest** of the 35k-row Takeout watch-history, grouped by category into separate notebooks | When you have a *concrete question* that the markdown folder can't answer. ON costs: 1 GB RAM, 3 ports held permanent, persistent volumes. |
| D2 | **Multi-account watch-history ingest pipeline** (sqlite, ~80 LOC) | When you actually have a *second Takeout* export from a second account. **2026-07-10 update**: per-URL analysis is automated as `~/bin/analyze.py`. Single-zip extract + sample + bulk analyze is automated as `~/bin/pipeline.py` (compose-only, dry-run tested). Multi-account sqlite consolidation = TBD. **O2 done in PM-3.** |
| D3 | **Classification layer** — embeddings + a categorization prompt, write results back to the `tags:` front-matter array | When you have 10+ analyzed videos AND can't tell which one is "AI tooling" vs "founder psychology" without reading each. |
| D4 | **Set-logic recall** — query across tags with union / intersection / complement | When you actually want to ask "videos about X AND not about Y." That question almost never comes; user-tested before building. |
| D5 | **Auto-fetch "similar videos"** via headless browser + dummy YouTube accounts | **Likely rung-1 reject.** YouTube ToS violation, account-suspension risk, CAPTCHA wall. **First fallback**: official YouTube Data API `related` endpoint per seed video — does the same job without bots. |
| D6 | **Persona-based agent arsenal** — multiple specialized sub-agents at spawn time | Mechanism already exists in Hermes via `delegate_task(toolsets=[...])` + `hermes profile`. Add when ONE concrete persona gap appears; instance-on-demand only, never tree-loaded. |
| D7 | **ON + Obsidian sync layer** | When (D1) is built AND you want ON's RAG alongside Obsidian's graph view. Cheap if both are file-shaped; revisit then. |
| D8 | **Takeout → ON bridge** (`~/bin/open-notebook-bridge/takeout_to_open_notebook.py`, stdlib-only, dry-run tested) | When (D1) gets a green light. Bridge already drafted; just needs `docker compose up` + a Takeout folder path. |
| D9 | **Multimodal-mode as default again** | When a real transcript-derived failure surfaces AND multimodal would clearly fix it (e.g. a video where the transcript is wrong / a slide deck matters more than speech). Until then, --multimodal stays opt-in behind a flag. |

## What we explicitly rejected today

- **Bulk-install of `lfnovo/open-notebook` skills or skills from `addyosmani/agent-skills`.** Skill catalogs are reference, not inventory. Add a skill when one specific skill solves a specific gap, never "just to have it."
- **Build a "persona system" before any single persona has a gap.** Mechanism exists; instantiate-on-demand.
- **Headless browser scraping as a default.** Use the official API; only fall back to a bot in a *targeted*, single-vendor scenario after API fails.
- **Multi-row Takeout parse today.** No second source yet.

## Resolved this session (notebook-style)

- **Embedding model**: stick with `nomic-embed-text-v1.5` (already in your Honcho stack, 768-dim). Don't A/B; cost vs benefit for English-transcript RAG is negligible.
- **LLM model for video analysis**:
  - Gemini path: `gemini-3.1-flash-lite` is the live one. `gemini-2.5-flash` 404'd for new-project users; `gemini-pro` 429'd on free tier. Keep `GEMINI_API_KEY` in `~/.hermes/.env`, prefix is `AQ.Ab8...` (current Gemini API key v2 format — `AIza` prefix is older).
- **ON chunking** (relevant if/when D1 ships): default `OPEN_NOTEBOOK_CHUNK_SIZE=400` is too small for long video transcripts. Override plan: `CHUNK_SIZE=1200`, `CHUNK_OVERLAP=120` (10% ratio, was 15%).
- **Time-window pre-filter**: when (D1) ships, prefer SQLite-level `watch_time` filter before embedding search — cuts noise cheaply.

## Open questions / friction watch

1. **Does the markdown folder actually answer your queries?** With 1 file: trivially yes. At 10 files: unknown. **At 10 files, re-read this doc and ask whether D3/D4 are pulling their weight.**
2. **Is classification worth the LLM cost?** Tag every video with one of: `ai-tooling | founder-psychology | investing | personal-development | other`. ~3 cents per video. Worth it only if retrieval fails.
3. **Multi-account friction?** When you hit it, do (D2) first; *then* consider D1.

## Adjacent work (on disk but inactive)

- `C:\Users\karee\bin\open-notebook-bridge\takeout_to_open_notebook.py` — bridge script, stdlib only, dry-run-tested, never run live. Ready for D1.
- `C:\Users\karee\AppData\Local\hermes\skills\media\youtube-analysis-multimodal-vs-transcript\SKILL.md` — saved A/B comparison skill, loaded only on demand.
- `~/open-notebook` — open-notebook repo cloned, never built/started. Read for chunking defaults + API surface, nothing more.
- `~/addyosmani-agent-skills` — repo cloned for reference, no skills imported.
- `C:\Users\karee\handoff-2026-07-10-open-notebook.md` — first handoff note (now superseded by this doc).

## What "done" looks like for *today*

**Session 2026-07-10 AM**:
- `M1E4ZzdpOco.md` — first analyzed video. ✓
- `CONVENTIONS.md` — format spec. ✓
- `REQUIREMENTS.md` — this file. ✓

**Session 2026-07-10 PM** (Takeout → extract-and-analyze pipeline, rung-7):
- Read `takeout-20250508T060843Z-001.zip` from `~/Downloads/`. ✓ (15 files, 2.3 MB, contains JSON `watch-history.json`)
- Parsed 42,400 records; 42,004 with YouTube `titleUrl`; 40,030 unique URLs. ✓
- Sample 6 URLs evenly spread May 2024 → May 2025. ✓
- Ran Gemini 3.1-flash-lite multimodal A/B on all 6 in 2 waves. 4 succeeded, 2 hit 429 quota + 1 hit a 403 permission error. ✓
- Sequential retry recovered the 429 case. ✓
- Wrote 4 markdown files (one per cleanly-analyzed video): `dJWFUBAUM0E.md`, `WliU1wBqF78.md`, `9nAB-AC5ngE.md`, `Gp0Q4O-CMZ4.md`. ✓
- Skipped 2 videos with documented reasons (Rl7S0U4_NwA = transcripts disabled + multimodal quota exhausted; sfP7ILlDCgU = multimodal 403 PERMISSION_DENIED + transcript is 358-char ad copy). ✓

**Session 2026-07-10 PM-2** (script lift, rung-7):
- Manual pattern repeated enough to justify `~/bin/analyze.py`. ~270 LOC stdlib + urllib. ✓
- Encoded failure modes observed earlier: 429 with exponential backoff, 403 PERMISSION_DENIED, transcripts-disabled fallback, low-signal skip. ✓
- Smoke-tested on `EaqEkSgUUBg` (mid-list Takeout URL). End-to-end: 4 shapes OK, file written. ✓
- All work matched `CONVENTIONS.md` format. Front-matter `title` is currently `(see URL)` placeholder — fine for manual fill-in, no oEmbed fetch in v1.

**Session 2026-07-10 PM-4** (transcript-by-default reorientation, rung-7):
- `analyze.py` flipped from multimodal-default to **transcript-default** mode. Multimodal now opt-in via `--multimodal`. ✓
- Added 4 transcript-mode prompt templates (`PROMPTS_TRANSCRIPT`) — text-LLM is told "you CANNOT see the video, reason only from transcript." ✓
- New `call_gemini_text(...)` posts to `gemini-flash-lite-latest` (separate quota bucket from `gemini-3.1-flash-lite` multimodal). 4 sequential calls per video, ~15s wall. ✓
- Smoke-tested on `wgOOBW3CJIY` (Muhammad Ali speech): all 4 shapes OK, file written, transcript-only markdown looks high-quality and timestamp-anchored to the actual video. ✓
- Skip-write behavior preserved: if transcript < 200 chars or unavailable, don't burn any quota; return code 1. ✓
- Multimodal quota (from earlier session) is exhausted for `gemini-3.1-flash-lite`, but text-mode quota on `gemini-flash-lite-latest` is in a separate bucket and unaffected. ✓
- Five existing multimodal-analyzed files left untouched (still valid; they cover cases where text alone was insufficient).

**Session 2026-07-10 PM-3** (pipeline composition, rung-7 O2):
- Lifted manual extract-and-sample pattern into `~/bin/takeout_sample.py` (~140 LOC, stdlib). ✓
- Wrote `~/bin/pipeline.py` (~115 LOC) composing `takeout_sample.py` + `analyze.py` via subprocess. ✓
- `pipeline.py --dry-run` smoke-tested end-to-end: sampled 6 URLs from the auto-picked zip, dry-run skipped Gemini calls; sample set reproducible because `takeout_sample.py` picks the last entry per month deterministically. ✓
- Live run *deferred* — Gemini free-tier quota exhausted earlier in the session (multiple 429s); running `pipeline.py` live would re-burn quota on URLs that may overlap with the 6 already on disk. Next session will run it after daily quota resets.

**Quoted constraint observed this session**:
- Gemini free tier: ~6 multimodal calls before quota-exhausted. The 8 hr/day free quota is real.
- Some videos: Gemini 3.1-flash-lite returns 403 PERMISSION_DENIED even with safety filter disabled — likely regional or content-policy. **No clean workaround within free tier.**
- Ponytail rung-1 held: 2 of 6 analyzed videos not persisted because signal was insufficient. Ad copy and disabled-transcripts are not worth the disk space.

**Sample picker** (manual this session, not yet a script):
- Bucket URLs by `YYYY-MM` from the `time` field.
- Pick one URL from oldest month with data, then 4 evenly-spaced middle months, then latest month.
- Result: 6 URLs spaced across a full year, not clustered in a single week.
- This is reusable code, ~30 LOC. Will write into `~/bin/open-notebook-bridge/takeout_sample.py` only when the manual pattern repeats (5+ times).

**What stays un-built today** (rung-1 holds):
- No docker containers started.
- No new processes.
- No new ports held.
- Bridge script (`takeout_to_open_notebook.py`) still not built (different problem — ON ingest, not per-URL analyze). **NOTE**: `analyze.py`, `takeout_sample.py`, `pipeline.py` were built and tested this session — see PM-2 and PM-3 sections above. **NEXT RUNG** is O5 (semantic search across the corpus) when you confirm the corpus is large enough to search.

## What "done" looks like for *next session*

Likely outcomes, in order of likelihood:

A. **"Add a `tags:` classifier, but only if I have 10 files."** → defer until then. Run grep across the folder to see growth.

B. **"Analyze 3 more videos before we go further."** → run `analyze.py <url>` (not yet built) or do it by hand.

C. **"Set up ON, ingest the Takeout, prove the bridge works end-to-end on 1 URL."** → the long-rung path. Confirm ~1 GB RAM + 3 ports cost is acceptable first.

D. **"Stand down again. Don't build more."** → re-evaluate next time. Always available.

---

## Pipeline architecture — full-app thinking (added 2026-07-10 PM-5)

**Context**: user asked for a pipeline that (a) processes all URLs in any
takeout without human intervention, (b) accepts URLs from any input format
(takeout zip, scraped text, URL list), (c) polls for new files every 20 min,
(d) is callable by other processes. The question isn't "build the whole
app" — it's "does today's pipeline shape support a larger system without
rewrites?"

### What today already supports

| Future need | Today's mechanism | Works without rewrite? |
|---|---|---|
| Chat with watch history + analyzed videos | Markdown folder + `analyzed.sqlite` index | ✓ |
| Multi-URL run | `analyze.py URL1 URL2 ...` (already accepts N args) | ✓ |
| Playlists as URL source | `--source takeout-watch` is one entry; add `--source playlist-url` = ~30 LOC | ✓ — one new source function, no core change |
| Selected URLs (chat with one chosen video) | `analyze.py <url>` accepts arbitrary input | ✓ |
| Cross-day resume | SQLite index already de-duplicates; revisit = no-op | ✓ |
| Multi-account | Schema is per-slug; no account column. Cross-account aggregation needs `account` column added | ✗ — schema migration needed |
| Channel discovery | Per-video markdown files have channel metadata in Takeout's `subtitles[]` field — currently discarded in `url_source.py`. Capture it. | ✓ as code change, ~10 LOC |
| External process triggers | CLI invocation = `subprocess.run(['python','pipeline.py',...])`. Already IPC. | ✓ — already works |

### What today does NOT support (and the rung-1 trigger for each)

| Future need | Trigger to build | Estimated LOC |
|---|---|---|
| **Resume across runs** (don't re-sample same N URLs; pick up where last run stopped) | When: user says "process all 40k" and runs `pipeline.py --all` once | ~30 LOC |
| **Format dispatcher** (`text-file`, `json-list` URL inputs alongside Takeout) | When: scraper produces URLs in a non-Takeout format | ~50 LOC |
| **Daemon mode** (poll `~/inbox/` every 20 min) | When: continuous input stream from another process exists | ~40 LOC |
| **Windows Task Scheduler / `hermes cron` registration** | When: daemon mode is built and user wants it to actually fire on a schedule | ~5 LOC (`schtasks /create /sc minute /mo 20 /tn pipeline /tr "..."`) |
| **Multi-account schema** (track which account each URL came from) | When: second Takeout export from a different Google account lands in Downloads | ~10 LOC (`ALTER TABLE analyzed_videos ADD COLUMN account TEXT`) |
| **Channel discovery from Takeout's `subtitles[]`** | When: corpus hits 50+ and user wants "channels I watch most" | ~10 LOC in `url_source.py` (already extracts title; add channel from `row['subtitles'][0]['name']`) |
| **CDC on YouTube state** (snapshot watch-history + playlist ETag; only ingest the diff since last poll) | When: a scraper exists that needs "only new since last poll" | ~50 LOC (ETag store + diff function + new `--source user-state-diff`) |
| **Push trigger from YouTube** (PubSubHubbub-style webhook listener that fires `pipeline.py` when a watched video / playlist change is detected) | When: external trigger matters more than polling | ~150 LOC (HTTP server + YouTube Data API key + OAuth + dedup) |
| **Seed-scrape tracking** (relate scraped-videos table to seed-table with timestamps + duration + counts) | When: a scrape job exists (D5 — currently rung-1 reject) | ~30 LOC |

### Explicit non-builds (gated on absence of evidence)

- A real HTTP API for external processes. **CLI is IPC for v1.**
- An event bus / queue library (Celery, Redis, RQ). **Stdlib file-watching is enough for v1.**
- A multi-process worker pool. **Single-process sequential is enough at current scale.**
- A daemon that runs forever. **Daemon-mode is opt-in via `--daemon` flag, exits cleanly when queue is empty.**

### The minimal extension path

If the user names *one* of the above triggers, the smallest code change is:

1. Resume flag (rung 1, ~30 LOC): add `--resume` to `pipeline.py`, read/write a small `pipeline-state.json` with `{last_processed_slug, total_processed}`.
2. Format dispatcher (rung 2, ~50 LOC): add `parse_url_file(path) -> list[url]` in `url_source.py`, dispatch on file extension.
3. Daemon mode (rung 3, ~40 LOC): add `--daemon` flag, loop with `time.sleep(1200)`, check `~/inbox/` directory.
4. Schedule (rung 4, ~5 LOC): register via `hermes cron` or Windows Task Scheduler.

Total: ~125 LOC. Stdlib only. No new deps. Each rung is gated on a concrete friction; nothing should be built ahead of evidence.

### How this fits a future chat-application

When (not yet decided) the chat interface lands, its read path is:

```python
from analyzed.sqlite: SELECT slug, url, mode, outcome, out_path
  WHERE analyzed_on >= ? AND outcome = 'ok'
read *.md from out_path column
```

That's already supported today. No pipeline changes needed for the chat
interface itself. The pipeline's job is to keep `analyzed.sqlite` and the
markdown folder populated — which it does, one URL at a time.

### Quota truth: "process all URLs in one go" (2026-07-10)

The pipeline supports a chronological scan over all 40k URLs in the
Takeout (`pipeline.py --resume --batch-size 41960`). Free-tier Gemini
quota is the binding constraint:

- Multimodal (`gemini-3.1-flash-lite`): ~6 calls/day before 429. **~17 years** to finish 40k URLs.
- Text (`gemini-flash-lite-latest`): wider bucket, observed ~50–200 calls/day before 429. **~6–18 months** to finish 40k.

Realistic run shape:

```bash
# Daily invocation; resumes at saved cursor; persists state across runs.
# State file: ~/.hermes/video-analysis/pipeline-state.json
python ~/bin/pipeline.py --resume --batch-size 50

# Or one-shot a big batch then sleep. Cursor advances per-attempted URL,
# so even unplayable videos count toward the cursor (no infinite loop).
python ~/bin/pipeline.py --resume --batch-size 200
```

To make this fire without manual intervention: register via `hermes cron`
or Windows Task Scheduler to run daily at quota reset. **Not built —
gated on the user actually wanting daily-firing.**

Honest answer to "process all URLs in one go without asking anyone":
- The pipeline supports it.
- Free-tier quota makes "one go" mean 6–18 months.
- Without a paid tier or local STT fallback (Tier 2 from
  `ANALYSIS-FALLBACK.md`), the constraint is the budget, not the code.

### Channel discovery (added 2026-07-10 PM-6)

`url_source.py` now lifts the channel name from Takeout's `subtitles[0].name`
field. Stdout is now 5-column (`id | url | ts | title | channel`).

To find "channels you watch most" without writing more code:

```bash
python ~/bin/takeout_sample.py --source takeout-watch-all --limit 41960 \
  | awk -F'|' '{print $5}' \
  | sort | uniq -c | sort -rn | head -20
```

This works *today*. No build needed for the aggregation; it's a one-liner.

**Note**: shorts and embeds often have empty `subtitles` arrays (the uploader
is unknown to YouTube's export). Those rows contribute `""` to the channel
column and show up as a single bucket in the aggregation. Trim them:

```bash
| grep -v '^[[:space:]]*$' | awk '$1 != ""'
```

### What this section IS NOT

- Not a build spec. Don't implement resume/daemon/dispatcher without a
  named trigger.
- Not a complete system design. The chat interface, scrapers, channel
  discovery — these are *unknown shapes today*. Build only the pieces the
  pipeline actually needs.
- Not a "we'll need this eventually" document. Each rung is gated.

---

This file is what you re-read when the context window forgets. Update it whenever a deferred item changes state.
