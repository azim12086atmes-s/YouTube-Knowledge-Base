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
                       ┌─────────────────────────────────────────────────┐
                       │              jobs.sqlite (audit log)             │
                       │                                                 │
   enqueue  ───────►   │  pending → awaiting-quota → running → ok/failed │
                       │  (idempotent on key_hash; every transition     │
                       │   written to jobs_log)                          │
                       └────────────────────────┬────────────────────────┘
                                                │
                                                ▼
                                       bin/daemon.py
                                       (sleep + dispatch loop)

  ┌──────────────┐    ┌────────────────┐    ┌──────────────────┐
  │ URL sources  │    │   analyze.py   │    │  vector_store.py │
  │              │    │                │    │                  │
  │ - takeout-   │───►│ 4-shape prompts│───►│ - chunk 120 wds  │
  │   watch zip  │    │  via Gemini    │    │ - embed MiniLM   │
  │ - xlsx       │    │  (text or      │    │   L6-v2 384 dim  │
  │ - urlfile    │    │   multimodal)  │    │ - sqlite-vec L2  │
  └──────────────┘    └────────┬───────┘    │   (= cosine)     │
                                │            └────────┬─────────┘
                                ▼                     │
                       ┌────────────────┐              │
                       │ ~/Documents/   │◄─────────────┘
                       │ video-analysis/│
                       │  <slug>.md     │
                       │  <slug>.tx.txt │
                       │ analyzed.sqlite│
                       └────────────────┘
                                ▲
                                │
                  ┌─────────────┼─────────────┐
                  │             │             │
              ask.py       chat.py       web.py
              (1-shot Q)   (REPL)        (FastAPI +
                                            inline HTML)
```

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
- Returned payload: `[{slug, idx, distance, text}]`.

The retrieval call site is one function:

```python
hits = _chat.retrieve_chunks(idx_path, question, k=8,
                            allowed_slugs=allowed_set)
```

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

The HTTP layer is a thin FastAPI wrapper around `chat.py` + `vector_store.py`.
HTML is one inline file in `web.py` with vanilla JS — no build step.
Tag filter is per-request (REST semantics), not per-session like the
REPL. Routes:

| method | path | purpose |
|---|---|---|
| GET    | `/` | the chat UI |
| POST   | `/api/query` | `{question, session_id?, k?, tag?}` → RAG answer + retrieved chunks |
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
- **Hybrid retrieval (BM25 + dense) + reranker.** The current cosine-
  top-k retrieval is good enough that we've measured no failures on
  real data. Add either when measured miss-rate becomes a complaint.
- **Click-through timestamp anchors.** NotebookLM citations can jump
  to a specific second in a video. This pipeline doesn't record
  timestamps in chunks. Adding it requires re-ingesting transcripts
  as JSON-with-start-times and updating the chunker.
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