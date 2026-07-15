# video-pipeline

A local-first pipeline that turns video URLs into a searchable,
question-answerable RAG corpus. Built around the standard 4-shape
analysis pattern (Summary / Key Takeaways / Non-Obvious Insights /
Revolutionary Reframes), indexed with `sqlite-vec`, and served
through a CLI REPL, an HTTP API, and a single-page browser UI.

## What it does

- **Ingest** URLs from a Google Takeout zip, an Excel/CSV/JSONL
  file, or a raw URL list. One file in, one queue of work out.
- **Analyze** each URL via Gemini: transcript-mode (default; cheap,
  ~3s wall) or multimodal-mode (Gemini watches the video; ~11s wall,
  free-tier-quota-limited).
- **Index** every output as Markdown (one file per slug) plus a SQLite
  vector store with cosine-retrievable chunks.
- **Query** the corpus from the CLI REPL, a FastAPI HTTP endpoint,
  or a self-contained browser chat. Tag-filter, session memory, and
  multi-turn conversation work in all three.

## What it is *not*

- Not a hosted service. There is no server you ship to users; the
  browser UI binds to `127.0.0.1` and the operator is the only reader.
- Not a video scraper or downloader. It consumes captions/transcripts
  YouTube already exposes; it does not download media.
- Not a general-purpose RAG framework. The chunker, retriever, and
  generator are tuned for one input shape (a YouTube video) and one
  output shape (a 4-shape markdown summary). Generalising requires
  a deliberate rewrite, not a config change.

---

## Architecture

```
   enqueue (jobs.py)
        │
        ▼
   jobs.sqlite ──► bin/daemon.py ──► bin/analyze.py
   (audit log)      (sleep loop)         │
                                         ├─── --multimodal ──► Gemini (video frames)
                                         │                          │
                                         │                          ▼
                                         ├─── --transcript ──► Gemini (4 prompts)
                                         │                          │
                                         │                          ▼
                                         │            ┌──────────────────────┐
                                         │            │  <slug>.md (4-shape) │
                                         │            │  <slug>.tx.txt       │
                                         │            │  analyzed.sqlite     │
                                         │            └──────────┬───────────┘
                                         │                       │
                                         │            vector_store.upsert_chunks
                                         │              (chunks / chunks_fts)
                                         │
                                         └─── --ingest-raw ─► youtube-transcript-api
                                                                │ (no Gemini call)
                                                                ▼
                                                   <slug>.md (stub) +
                                                   <slug>.transcript.jsonl
                                                   (timestamped, BM25-indexed)
                                                                │
                                                                ▼
                                                        chunks with start_s/end_s
                                                        → /api/pinpoint click-to-second
```

The two paths converge at `vector_store.upsert_chunks[_with_timestamps]`:
both produce rows in `chunk_meta` + `chunks` (vec0) + `chunks_fts` (FTS5)
+ an `analyzed_videos` row. The corpus is *one* index regardless of
which path produced the row; the `outcome` column tells them apart
(`ok` for the 4-shape path, `ok-raw` for `--ingest-raw`).

The single retrieval function `chat.retrieve_chunks(...)` decides at
query time: `mode='hybrid'` (default, RRF of dense + BM25),
`mode='dense'` (legacy), or `mode='pinpoint'` (BM25-only on a phrase).
Generation (`ask.py` / `chat.py` / `web.py` `/api/query`) is a single
prompt shared by all three call sites; only the retrieval-feeding
step differs.

Components:

| Path | Role |
|---|---|
| `bin/url_source.py` | URL dispatch: Takeout zip, xlsx, txt/jsonl. Stdlib-only. |
| `bin/pipeline.py` | Composes `url_source.py` + `analyze.py`; cursor state for `--resume`. |
| `bin/analyze.py` | One URL → four-shape Markdown; transcript or multimodal mode. |
| `bin/vector_store.py` | Embedding + chunking + cosine retrieval + chat/tag persistence. |
| `bin/ask.py` | Single-turn RAG: vector search → prompt → Gemini. |
| `bin/chat.py` | REPL: same prompt pipeline + per-session history + tag filter. |
| `bin/web.py` | FastAPI HTTP wrapper around `chat.py`; vanilla-JS UI inline. |
| `bin/jobs.py` | SQLite-backed job queue with audit log; idempotent on `key_hash`. |
| `bin/daemon.py` | Long-running dispatcher; periodic + resumable. |
| `bin/kanban.py` | TTY readout of `jobs.sqlite` by lifecycle state. |
| `bin/list.py` | Inventory CLI over the corpus index. |
| `bin/backfill_watched_at.py` | One-shot repair for `watched_at:` front-matter. |
| `bin/takeout_sample.py` | Back-compat alias of `url_source.py`. |

---

## RAG design

The RAG pipeline is the load-bearing piece. Two design choices drive
every other decision: chunking at the sentence window with a 20-word
overlap, and ranking by cosine similarity on unit-normalised
embeddings.

### Ingestion

A source is any iterable of `{id, url, ts?, title?, channel?}` records.
`url_source.py` produces these from a Takeout zip (parse
`watch-history.json`, dedupe by `(vid, ts)`), an `.xlsx` (zipfile +
ElementTree, no openpyxl dep), or a `.txt`/`.jsonl` URL list. The
metadata fields are preserved through the pipeline so the markdown
front-matter can carry `watched_at:` from the Takeout `time` field.

### Chunking

```python
# vector_store.py: chunk_text()
CHUNK_WORDS = 120
CHUNK_OVERLAP_WORDS = 20
```

120 words is roughly 480 tokens with the MiniLM tokenizer — small
enough that `k=10` chunks plus the system prompt fit comfortably in
Gemini's input budget with room for the question and a long answer.
The 20-word overlap preserves continuity at chunk boundaries.

If the source has a transcript sidecar, chunks embed the transcript.
If the source is a multimodal-mode analysis with no transcript, chunks
embed `## 1. Summary` + `## 2. Key Takeaways` from the markdown body.
Both branches live in `vector_store.body_text_for_indexing()`.

### Embedding + retrieval

- Model: `sentence-transformers/all-MiniLM-L6-v2` (384-dim, cosine,
  ~80 MB on disk).
- Index: `sqlite-vec` virtual table `chunks` keyed by `chunk_meta`
  rowid. WAL mode for safe concurrent reads.
- Distance: L2 on unit-normalised vectors. Equivalent to cosine rank
  for unit vectors, and `sqlite-vec` has L2 as its native distance.
- Top-k: 8 for the chat REPL, 10 for `--all`. `--tag <t>` halves
  `fetch_k` post-filter inside the slugs matching that tag.
- Returned payload: `[{slug, idx, distance, text, start_s, end_s, score}]`.

**Hybrid dense + BM25** (D27, the default since 2026-07-14): the
retriever runs both a dense top-k (`chunks` vec0 by L2) and a BM25
top-k (`chunks_fts` FTS5) and fuses them with Reciprocal Rank Fusion
(Cormack et al. 2009, k=60). The two lists overlap on the corpus
anchor chunks but diverge on:

- **Dense wins on paraphrase** — "fix memory leak" matches
  "diagnose RAM consumption" because the embedding geometry is
  smoothed.
- **BM25 wins on identifier lookup** — proper nouns, model numbers,
  error codes, YouTube slugs. The exact-token match is what you want
  here, and dense averaging eats it.

Why RRF: it doesn't require learning weights, it doesn't assume the
two scorings are calibrated, and it converges on the chunks that
*both* lists agree are relevant. Anthropic's contextual-retrieval
benchmarks (Sep 2024) report a 49% retrieval-failure reduction from
the dense+BM25 pair alone (without reranker) versus 35% from
contextual embeddings alone.

**Schema:**

```
chunk_meta (rowid, slug, idx, text, start_s REAL, end_s REAL)
chunks      (vec0 virtual — embedding float[384])
chunks_fts  (FTS5 virtual — text, slug UNINDEXED, idx UNINDEXED,
             tokenize='porter unicode61 remove_diacritics 2')
```

`ensure_vec()` is idempotent: it adds the new columns via
`ALTER TABLE … ADD COLUMN` (swallowing `duplicate column name`),
creates the FTS5 table on first call, and backfills it from
`chunk_meta` if any chunks pre-date the FTS5 index. After the first
call, every `upsert_chunks` writes to all three tables in one
transaction so a partial failure can't leave orphans.

The retrieval call site is one function with a `mode` parameter:

```python
# chat.retrieve_chunks(idx_path, question, k=8,
#                      allowed_slugs=set, mode='hybrid'|'dense'|'pinpoint')
hits = _chat.retrieve_chunks(idx_path, question, k=8,
                            allowed_slugs=allowed_set)
```

`mode='hybrid'` is the default (D27+). `mode='dense'` is the legacy
path kept for back-compat. `mode='pinpoint'` is BM25-only on an
exact phrase — used by `/api/pinpoint` for the click-to-second UX.

### Generation

The prompt template (in `chat.py:build_contents`) is:

```
You are a research assistant for a personal YouTube Knowledge Base.
Answer the user's question using ONLY the provided transcript excerpts.
Quote specific moments (with the slug + a verbatim phrase) to support
each claim. If the transcripts don't address the question, say so
explicitly.

Retrieved transcript excerpts (N):
[<slug> (dist=X.XXX)] <chunk text>
...

[conversation history, capped at 8 messages]

[current user question]
```

Two anti-hallucination guarantees are enforced *in the prompt*:

1. "Use ONLY the provided transcript excerpts" — model is told to
   refuse rather than invent when no excerpt addresses the question.
2. "Quote (slug + verbatim phrase)" — every substantive claim must
   cite a slug and an actual phrase from the excerpt.

The same prompt template is shared by `ask.py`, `chat.py`, and
`web.py`. Three call sites, one prompt.

The default model is `gemini-3.1-flash-lite` (chat REPL + HTTP) or
`gemini-flash-lite-latest` (analyze.py text path). Multimodal-mode
analysis uses `gemini-3.1-flash-lite`. Models are documented in
`bin/analyze.py`; switching is a one-line constant change.

### Retrieval modes (vector vs. url-list)

Two retrieval paths share the same generation prompt:

| Mode | When used | Source | Trade-off |
|---|---|---|---|
| `vector` | default; no slug list given | top-k cosine-similar chunks from the SQLite-vec index | scales to any corpus size; misses exact-phrase and rare-keyword queries |
| `url-list` | user explicitly picks slugs (Web UI sidebar, `ask.py <url1> <url2>`, or `/api/query` with `slugs=[…]`) | full transcript text per chosen slug, bundled into the prompt | best when the user knows the relevant sources; degrades when bundle size exceeds LLM context |

**Budget rule.** The url-list path exposes a `per_slug_chars`
parameter (default 60 000 chars per slug). 6 transcripts × 60 000 chars
= 360 000 chars ≈ 100 000 tokens, well within Gemini 3.1 Flash's
1M-token context. When the bundle gets larger, lower the per-slug cap
or fall back to vector search.

**Honest refusal.** Both modes include the prompt instruction
"If the transcripts don't address the question, say so explicitly."
The model is told *not* to invent when no excerpt is on-target.

### Architectural mapping vs. NotebookLM

For a side-by-side of this pipeline against Google NotebookLM —
which features we transfer, which we deliberately skip, and why — see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Install

```bash
git clone https://github.com/azim12086atmes-s/YouTube-Knowledge-Base
cd YouTube-Knowledge-Base

# Python deps (pinned to the versions used in development)
pip install -r requirements.txt

# API key
echo 'GEMINI_API_KEY=YOUR_KEY_HERE' >> ~/.hermes/.env
# Get a key at https://aistudio.google.com/apikey
```

The repo is portable across Linux, macOS, and Windows. The corpus
output path defaults to `~/Documents/video-analysis` on every
platform; the SQLite DB sits next to the markdown files.

## Quickstart

```bash
# 1. Drop a Takeout zip in ~/Downloads/ (or a .xlsx/.txt URL list)

# 2. Sample + analyze 6 URLs
python bin/pipeline.py

# 3. Ask a question across the corpus
python bin/ask.py --all --question "what themes recur across my watched videos?"
```

That's the happy path. Every other command in this README is a
refinement on top of these three.

---

## Usage

### Analyze a single URL

```bash
python bin/analyze.py "https://www.youtube.com/watch?v=<id>"
```

Default mode is transcript-only (~3s wall per video, no quota burn
on multimodal). `--multimodal` switches to "Gemini watches the video"
mode (~11s wall, free-tier quota-limited). The analyzer skip-writes
on transcripts that are missing or junk, and dedupes via the SQLite
index so re-running the same URL is a no-op.

### Ingest raw transcripts (no Gemini call)

```bash
python bin/analyze.py "https://www.youtube.com/watch?v=<id>" --ingest-raw
# or for the long Takeout walk:
python bin/pipeline.py --source takeout-watch --batch-size 50
```

`--ingest-raw` is the **fast path**. It fetches the YouTube transcript
directly, embeds it locally with `sentence-transformers`, and writes
to the vector index. **No Gemini call is made.** The 4-shape markdown
summary is *not* produced — the slug is searchable through the index
and FTS5 immediately, but has no human-readable analysis body. A
subsequent `--multimodal` pass on the same slug can add a deep-dive
summary for videos you actually want to study.

The 40k-URL Takeout walk: at free-tier quota (50–200 Gemini calls
/day) the 4-shape path is 6–18 months. The `--ingest-raw` path is
bounded only by YouTube's caption-fetch rate limit (~200 req/min)
and local embedding compute (~1s per chunk on CPU). Hours, not
months.

Each raw-ingested slug also writes a `.transcript.jsonl` sidecar
(`{text, start, end}` per line) so chunks carry real YouTube
timestamps and `/api/pinpoint` returns click-to-second YouTube URLs.
`--force` re-embeds an already-indexed slug (use this after a
chunker fix or to refresh transcripts that updated on YouTube).

### Ingest from a Takeout zip

```bash
# Sample 6 URLs evenly across the date range, analyze each
python bin/pipeline.py

# Walk the corpus in chronological order; resume across runs
python bin/pipeline.py --resume --batch-size 50
```

State file: `~/.hermes/video-analysis/pipeline-state.json` tracks the
cursor across runs. Free-tier Gemini quota is the binding constraint
(50–200 text-mode calls/day); `--resume` makes the long walk
trivially resumable across quota resets.

### Ingest from xlsx / txt / jsonl URL lists

```bash
# xlsx: auto-discovers ext*cted_youtube_urls*.xlsx in ~/Downloads/
python bin/pipeline.py --source xlsx

# txt / jsonl / raw URL list: file is the path
python bin/url_source.py --source urlfile --file my_urls.txt --n 50
```

Stdlib-only (zipfile + ElementTree). No `openpyxl` dep. The xlsx parser
handles both `sharedStrings.xml` and inline-string cells.

### Ask a question

```bash
# Across chosen transcripts
python bin/ask.py <url1> <url2> --question "what did these speakers say about war?"

# Across the entire corpus (vector search + Gemini)
python bin/ask.py --all --question "what themes run across my watched videos?"

# Restricted to one tag
python bin/ask.py --all --tag ai-tooling --question "what does this speaker say about building software?"

# See the raw retrieved chunks before the LLM answer
python bin/ask.py --all --question "conscience and war" --show-chunks
```

Honest refusal is built into the prompt: if no excerpt addresses the
question, the model says so rather than confabulating.

### Pinpoint search (find the second where X said Y)

```bash
# CLI: not yet; the Web UI is the primary surface.
# API:
curl "http://localhost:8080/api/pinpoint?phrase=conscience"
# → {"hits": [{"slug": "wgOOBW3CJIY", "start_s": 1.36, "end_s": 47.16,
#              "youtube_url": "https://youtu.be/wgOOBW3CJIY?t=1", ...}]}
```

Pinpoint is **BM25-only on an exact phrase** (mode='pinpoint'). It
returns every chunk containing that phrase, ordered by BM25, with
the real YouTube `start_s`/`end_s` for each hit. Each hit's
`youtube_url` is a click-through link to `?t=<start_s>` so the
browser jumps to the second of the video where the speaker said
the phrase.

This is the dense-retrieval-failure antidote: when the user knows
the exact phrase ("the model TS-999", "YH4zaMAnGWs", "dQw4w9WgXcQ"),
BM25 catches it instantly. The dense path's strength is paraphrase
("fix memory leak" → "diagnose RAM consumption"), but it loses
exact-token matches. Pinpoint is the tool for those.

Pinpoint requires timestamps (`start_s`/`end_s` not NULL) on the
chunks. Today's corpus has 13 timestamped chunks from a single
`--ingest-raw` slug; the other 111 chunks pre-date the timestamp
schema and surface in pinpoint results *without* a clickable URL
(their `youtube_url` is `null`). Re-ingest any slug with
`--ingest-raw --force` to add timestamps without re-prompting Gemini.

### Multi-turn chat

```bash
python bin/chat.py                       # default session
python bin/chat.py --session mychat     # named session
```

Commands inside the REPL:

| command | what |
|---|---|
| `:quit` | exit (Ctrl-D / Ctrl-Z also works) |
| `:clear` | wipe session history |
| `:status` | session id + message count + last retrieval summary |
| `:show` | print last retrieved chunks |
| `:history` | dump raw conversation history |
| `:sessions` | list all sessions |
| `:tag [name]` | set/show the active tag filter (per-session) |

### Web UI

```bash
python bin/web.py --port 8080
# then open http://localhost:8080
```

The HTTP layer is a thin FastAPI wrapper around `chat.py` + `vector_store.py`
+ `ask.py`. HTML is one inline file in `web.py` with vanilla JS — no
build step. Tag filter is per-request (REST semantics), not per-session
like the REPL.

The left sidebar lists every analyzed video with mode / outcome / tags
and a small "transcript ✓" / "no transcript" indicator. Use it to pick
the slugs you want to ask about; the next `/api/query` will bundle
exactly those transcripts into the prompt. With no slugs selected, the
request falls back to vector search across the whole corpus. The
"mode: vector" / "mode: url-list" banner in the header tells you which
path each request took.

Routes:

| method | path | purpose |
|---|---|---|
| GET    | `/` | the chat UI (sidebar + log + input + pinpoint bar) |
| POST   | `/api/query` | `{question, session_id?, k?, tag?, slugs?, per_slug_chars?}` → RAG answer; `slugs` triggers url-list mode |
| GET    | `/api/videos` | catalog with mode/tag/outcome filters + `has_transcript` |
| GET    | `/api/transcripts/{slug}` | preview snippet (default 800 chars) |
| GET    | `/api/pinpoint?phrase=…` | BM25 exact-phrase search; each hit carries `start_s`/`end_s` and a `youtube_url` for click-to-second jump |
| GET    | `/api/sessions` | list sessions |
| GET    | `/api/sessions/{id}` | history + active tag |
| DELETE | `/api/sessions/{id}` | clear history + tag |
| POST   | `/api/sessions/{id}/tag` | `{tag}` → set or clear (null = clear) |
| GET    | `/healthz` | liveness |

### Classify and filter by tag

Each analyzed video is classified into one of nine fixed tags:
`ai-tooling`, `founder-psychology`, `investing`, `personal-development`,
`religion-or-faith`, `history-or-politics`, `music-or-performance`,
`lifestyle-or-cooking`, `other`. Tags are persisted in a SQLite table
and written back to the markdown front-matter.

```bash
# Tag as part of a fresh analyze
python bin/analyze.py "https://youtu.be/<id>" --classify

# Re-tag every existing markdown (one Gemini call each)
python bin/analyze.py --reclassify

# Re-tag from markdown body — covers multimodal-mode files
# that have no transcript sidecar
python bin/analyze.py --reclassify-from-md
```

Tag-filtering at query time:

```bash
# Single tag
python bin/ask.py --all --tag ai-tooling --question "..."

# Union — either tag matches
python bin/ask.py --all --tag investing --tag founder-psychology --question "..."
```

### Inspect the corpus

```bash
python bin/list.py                       # every analyzed video
python bin/list.py --tag ai-tooling     # one (or many) tags
python bin/list.py --mode multimodal    # only multimodal-mode analyses
python bin/list.py --outcome skip-junk  # only skips
python bin/list.py --limit 10           # last 10 analyzed
```

Output: one row per slug with `mode | outcome | tags | analyzed_on`.
Filters compose: `--tag x --tag y` is union.

### Job queue, daemon, and Kanban

For unattended operation across hundreds of URLs, the pipeline has
three pieces:

| Script | Role |
|---|---|
| `bin/jobs.py` | SQLite-backed job queue: `init`, `enqueue`, `list`, `show`, `dispatch`. Every state transition is recorded in an audit log (`jobs_log`). Idempotent re-enqueues are keyed by `key_hash`. |
| `bin/daemon.py` | Long-running dispatcher. Polls the queue every N minutes (default 20m), runs pending+awaiting-quota jobs, exits on `--once` or `--limit` exhaustion. Stale `running` rows are reclaimed on startup. |
| `bin/kanban.py` | TTY Kanban of every job in the queue. Columns by state: `pending`, `pending-approval`, `awaiting-quota`, `running`, `ok`, `skipped`, `failed`. `--watch --interval 5s` for live tail. |

```bash
# One-shot: enqueue a batch + dispatch immediately
python bin/jobs.py enqueue analyze url1 url2 url3
python bin/jobs.py dispatch --limit 3

# Long-running: drain the queue every 20 minutes
python bin/jobs.py daemon --interval 20m

# Watch the queue
python bin/kanban.py --watch --interval 5s
```

The daemon doesn't *decide* what to enqueue — it runs what was
intentionally queued. Concrete frictions land in the queue via
`bin/jobs.py enqueue` or via a cron-fronted CLI that enqueues
based on a rule. See **Future scope** for the rationale.

### Recovery operations

```bash
# Re-run every "skipped" row (uploader captions sometimes re-appear
# days after the first try)
python bin/analyze.py --retry-skips

# Backfill the SQLite vector index from existing markdown files.
# One way to recover from a corrupt or missing vector index.
python bin/analyze.py --reindex-from-md

# Backfill `watched_at:` front-matter from a Takeout zip
python bin/backfill_watched_at.py --zip path/to/takeout-2024xxxxx.zip
```

---

## Future scope

The following are explicitly **not built** and the reason is in
[`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md). Briefly:

- **Hosted multi-corpus service.** This is a single-operator, local-
  first pipeline. No auth, no quotas, no multi-tenant. Adding a
  hosted variant would be a different product, not a feature.
- **Reranker (Cohere Rerank 3 / BGE-reranker-v2-m3).** The hybrid
  dense+BM25 retrieval is the 49% lift from Anthropic's contextual-
  retrieval benchmarks; a reranker would push it to 67%. Held until
  measured miss-rate on real queries becomes a complaint.
- **Contextual embeddings (Anthropic's 35% lift).** Adds a per-chunk
  LLM call at index time. Cheap at 75 chunks, expensive at 10k+.
  Held.
- **Audio Overview.** Out of scope by design (hosted-only, Google-
  side). The local transcript is the closest substitute.
- **Headless-browser "similar videos" scraping.** YouTube ToS
  violation + account-suspension risk. The official YouTube Data API
  `related` endpoint is the honest alternative if a similar-videos
  use case fires.
- **Cron-style "self-prompting agent" loop.** Building an LLM-on-
  timer that decides what to enqueue is the kind of unbounded pattern
  that pages you at 3am with hallucinated work. Concrete frictions
  land in the queue via `bin/jobs.py enqueue` or cron-fronted CLI
  scripts that enqueue based on a rule. The daemon's job is to run
  what was intentionally queued.

> **Note** Items that *were* in earlier drafts of this section have
> shipped since: hybrid dense+BM25 retrieval (D27, 2026-07-14),
> click-through timestamp anchors via `/api/pinpoint` (D27), and
> the transcript-only ingest path `--ingest-raw` (D28). See the
> **RAG design** section above for the current architecture.

### Next-rung ideas (not built yet)

These are explicit rung-1 candidates that haven't fired:

- **Self-bootstrapping embeddings.** Re-embed chunks when the embedding
  model is upgraded, gated by `key_hash` so the work is idempotent.
- **Per-slug time-budget knobs.** `--batch-time-cap <min>` for the
  daemon: stops dispatching when the day is up, marks the rest
  `awaiting-quota`.
- **Approval gates.** `--needs-approval` already exists in
  `bin/jobs.py enqueue`. A worker that requires manual sign-off before
  running is a small follow-on.

---

## Project shape

```
video-pipeline/
├── bin/
│   ├── _gemini.py                  # shared POST helper for analyze/ask/chat
│   ├── analyze.py                  # 1-URL → 4-shape Markdown + SQLite row
│   ├── ask.py                      # single-turn RAG over chosen/all transcripts
│   ├── backfill_watched_at.py      # repair watched_at front-matter from Takeout
│   ├── chat.py                     # multi-turn REPL with history + tag filter
│   ├── daemon.py                   # periodic dispatcher (--interval 20m)
│   ├── end_to_end_check.py         # 46-probe runnable check that everything works
│   ├── jobs.py                     # SQLite-backed ops queue with audit log
│   ├── kanban.py                   # TTY Kanban readout of jobs.sqlite
│   ├── list.py                     # corpus inventory CLI
│   ├── pipeline.py                 # Takeout → sample → analyze; supports --resume
│   ├── takeout_sample.py           # back-compat alias of url_source.py
│   ├── url_source.py               # URL dispatch: takeout/xlsx/urlfile
│   ├── vector_store.py             # chunk + embed + cosine search + chat/tag persistence
│   └── web.py                      # FastAPI HTTP wrapper around chat.py
├── docs/
│   ├── ANALYSIS-FALLBACK.md        # 3-tier fallback design for transcripts
│   ├── ARCHITECTURE.md             # side-by-side with Google NotebookLM
│   ├── CONVENTIONS.md              # Markdown front-matter schema
│   └── REQUIREMENTS.md             # status of every feature + triggers
├── corpus → ~/Documents/video-analysis    # symlink: outputs land here
├── requirements.txt                # pinned deps (google-genai, sentence-transformers, sqlite-vec, …)
├── README.md                       # this file
└── LICENSE                         # MIT
```

---

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — what this pipeline
  takes from Google NotebookLM's RAG, what it skips, and why.
- [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) — status of every
  feature: built, deferred, rejected. Each deferred item names its
  rung-1 trigger.
- [`docs/CONVENTIONS.md`](docs/CONVENTIONS.md) — the Markdown front-
  matter schema every `<slug>.md` follows.
- [`docs/ANALYSIS-FALLBACK.md`](docs/ANALYSIS-FALLBACK.md) — 3-tier
  fallback design for transcripts.

---

## License

MIT. See [`LICENSE`](LICENSE).