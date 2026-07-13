# video-pipeline

A local-first YouTube video analysis pipeline that turns a Google Takeout
zip into a searchable, question-answerable corpus of plain-text summaries.

* Reads a Takeout zip, finds YouTube watch-history entries.
* For each entry, fetches the YouTube transcript and runs a 4-shape
  analysis (Summary / Key Takeaways / Non-Obvious Insights / Revolutionary
  Reframes) using Gemini text-mode LLMs.
* Stores the output as Markdown files (one per video) in a local folder.
* Stores a SQLite index of what was analyzed, when, and with which model.
* Lets you ask questions across any subset of transcripts — including the
  entire corpus — using Gemini as the answering model.

## What this is *not*

* Not a hosted service. Runs entirely on your machine.
* Not a video downloader / scraper. Reads what already exists in a Takeout
  zip.
* Not a web UI. `chat.py` is a CLI REPL. A browser-based chat is a future
  rung gated on user request.

## What it does today

| Capability | Status |
|---|---|
| Read Takeout zip → sample N URLs → analyze each | ✓ |
| Read Takeout zip → walk the entire corpus in chronological order with a state file | ✓ |
| Skip-write on transcripts that are missing / junk / disabled | ✓ |
| De-duplicate re-runs via SQLite index | ✓ |
| Ask a question across any subset of transcripts | ✓ (`ask.py URL1 URL2 ...`) |
| Ask a question across the entire corpus | ✓ (`ask.py --all`) |
| Channel discovery from Takeout's `subtitles[]` field | ✓ (`awk -F'|' '{print $5}' | sort | uniq -c | sort -rn`) |
| Multi-account Takeout | ✗ — gated on second Takeout export from a different account |
| Chat interface (multi-turn, conversation memory, UI) | ✓ — `bin/chat.py` REPL. Per-session history persisted in `chat_messages` table; retrieval + history injection per turn. CLI today; UI is a future rung gated on user request. |
| Chat `:tag` filter (per-session, applied to retrieval) | ✓ — `chat.py :tag <name>` sets, `:tag` shows. Filter persisted in `session_state` table. Retrieval re-ranks inside the active tag's slugs. |
| Corpus inventory / list CLI | ✓ — `bin/list.py [--tag] [--mode] [--outcome] [--limit]` queries `analyzed_videos` + `tag_assignments` in one call. Surfaces what's analyzed, by tag, by mode, by outcome. |
| Query raw transcript chunks verbatim | ✓ — `ask.py --show-chunks` prints top-k retrieved excerpts with slug + cosine distance before the LLM answer |
| Classification (auto-tag into fixed vocab) | ✓ — `analyze.py --classify` / `--reclassify`. 9-tag vocabulary (ai-tooling, founder-psychology, investing, personal-development, religion-or-faith, history-or-politics, music-or-performance, lifestyle-or-cooking, other). |
| Set-logic recall (union/intersection across tags) | ✓ — `ask.py --tag <t>` (repeatable: `--tag a --tag b` = either); tag CRUD in `tag_assignments` table. |
| Vector store / semantic search | ✓ — `sqlite-vec` + `sentence-transformers/all-MiniLM-L6-v2` (384-dim). `ask.py --all` uses cosine top-k; explicit-URL mode falls back to bundle-and-ask when the index has no embeddings. |
| Continuous extraction daemon (poll every 20 min) | ✗ — gated on a continuous input source |
| Push trigger from YouTube webhooks | ✗ — gated on a real external process |
| Headless-browser auto-fetch of similar videos | ✗ — rung-1 reject (YouTube ToS) |

## Install / setup

```bash
# 1. Clone
git clone <repo-url> video-pipeline
cd video-pipeline

# 2. Python deps — pinned to the exact versions used in development.
#    (For historical / reproduction purposes; in practice `pip install
#    -U` of these names is fine.)
pip install -r requirements.txt
# Or, equivalently, the un-pinned form (still in the README's "what to
# install" narrative, but require the file from here on):
#   pip install google-genai youtube-transcript-api sentence-transformers sqlite-vec

# 3. API key
echo 'GEMINI_API_KEY=YOUR_KEY_HERE' >> ~/.hermes/.env
# Get a key at: https://aistudio.google.com/apikey

# 4. (Optional) Drop a Google Takeout zip in ~/Downloads/
#    go to https://takeout.google.com → YouTube → history → JSON format

# 5. (Optional) Backfill embeddings from existing transcripts:
#    python bin/analyze.py --reindex-from-md

# 6. Verify everything works end-to-end:
python bin/end_to_end_check.py
# Should print "OK  all end-to-end checks passed" against the bundled
# corpus (3 transcripts with sidecars, 75 indexed chunks). If a check
# fails, the README's "Verify it works" section explains each one.
```

### Verify it works

After install, run the end-to-end check to confirm the pipeline works
against the bundled corpus:

```bash
python bin/end_to_end_check.py
```

It probes 11 things in ~10 seconds, using the live corpus + Gemini API:
corpus files exist, vector index populated, CLI surfaces present,
`ask.py --all` returns a real cited answer, `--show-chunks` works,
`chat.py` REPL persists a turn.

The bundled corpus has 8 markdown files (mixed multimodal + transcript-mode)
but only 3 transcript sidecars — multimodal-mode analyses don't produce
`.transcript.txt` sidecars (Gemini multimodal returns a 4-shape Markdown
but doesn't save the raw transcript). Only transcript-mode analyses do.
The check counts both.

The scripts assume:

- `~/Documents/video-analysis/` exists and is writable (auto-created on first run).
- `~/hermes/...` paths for the env file (Windows) or `~/.hermes/.env` (POSIX).

## Usage

### Analyze a single YouTube URL

```bash
python bin/analyze.py "https://www.youtube.com/watch?v=<id>"
```

Default: transcript-only mode (cheap, ~3s wall per video, no quota burn
on multimodal). Use `--multimodal` for Gemini to actually watch the video
(~11s wall, free-tier quota-limited).

### Process a Takeout zip end-to-end

```bash
# Sample 6 URLs evenly across the date range, then analyze each
python bin/pipeline.py

# Walk the entire corpus in chronological order; resume across runs
python bin/pipeline.py --resume --batch-size 50
```

State file: `~/.hermes/video-analysis/pipeline-state.json` tracks cursor
across runs. Run `--resume` repeatedly to chew through 40k watch-history
entries at Gemini free-tier pace (50–200 calls/day → ~6–18 months).

### Ask a question

```bash
# Across chosen transcripts
python bin/ask.py <url1> <url2> --question "what did these speakers say about war?"

# Across the entire corpus
python bin/ask.py --all --question "what themes run across my watched videos?"
```

Honest refusal is built in — if no transcript addresses your question, the
model will say so rather than confabulate.

### Multi-turn chat (CLI REPL)

```bash
python bin/chat.py                      # default session
python bin/chat.py --session mychat    # named session
```

Each turn: vector-search the corpus, prepend top-k excerpts to the
prompt, then call Gemini with the last 8 messages of conversation
history (4 user+model pairs). History is persisted in the same SQLite
file. Commands:

| command | what |
|---|---|
| `:quit` | exit (Ctrl-D / Ctrl-Z also works) |
| `:clear` | wipe session history |
| `:status` | session id + message count + last retrieval summary |
| `:show` | print last retrieved chunks (same as `ask.py --show-chunks`) |
| `:history` | dump raw conversation history |
| `:sessions` | list all sessions in the DB |

CLI today. A web UI is a future rung, gated on user request.

To see the *raw* retrieved transcript excerpts (slug + cosine distance +
verbatim text) before the LLM answer:

```bash
python bin/ask.py --all --question "conscience and war" --show-chunks
```

Useful when the LLM-rendered answer loses detail that the raw chunk
preserves, or when you want to quote a video directly without going
through the model.

### Classify + filter by tag

Each analyzed video gets classified into one of nine fixed tags:
`ai-tooling`, `founder-psychology`, `investing`, `personal-development`,
`religion-or-faith`, `history-or-politics`, `music-or-performance`,
`lifestyle-or-cooking`, `other`. Tags are persisted in a SQLite table
and written back to the front-matter of the analysis file.

```bash
# Tag as part of a fresh analyze:
python bin/analyze.py "https://youtu.be/<id>" --classify

# Re-tag every existing markdown in the corpus (one Gemini call each):
python bin/analyze.py --reclassify

# Ask a question, restricting to a single tag:
python bin/ask.py --all --tag ai-tooling --question "what does the speaker say about building software?"

# Ask across multiple tags (union — either tag matches):
python bin/ask.py --all --tag investing --tag founder-psychology --question "what themes repeat?"
```

`--tag` requires the SQLite index to exist; `analyze.py --reindex-from-md`
populates it.

### List what's in the corpus

```bash
python bin/list.py                       # every analyzed video
python bin/list.py --tag ai-tooling     # filter to one (or many) tags
python bin/list.py --mode multimodal    # only multimodal-mode analyses
python bin/list.py --outcome skip-junk  # only skips (e.g. transcript-disabled)
python bin/list.py --limit 10           # last 10 analyzed
```

Output is one row per slug with `mode | outcome | tags | analyzed_on`.
Filters compose: `--tag x --tag y` is union (slug matching either). Useful
for "what do I actually have right now?" without opening Obsidian.

### Tag-filter the chat REPL

Inside `bin/chat.py` you can pin a session to one tag and every retrieval
will be re-ranked to top-k chunks from slugs carrying that tag:

```
chat: session='default' ... commands: :quit :clear :status :show :history :sessions :tag [name]
[default] > :tag ai-tooling
active tag set to 'ai-tooling' (1 slug(s) match)
[default] > what does this speaker say about building software?
... answer drawn from M1E4ZzdpOco only ...
[default] > :tag
active tag: ai-tooling
```

`:tag [name]` sets, bare `:tag` shows. New sessions default to no filter.
State is per-session, persisted in `session_state`.

### Backfill watch-time into front-matter

If you have a fresh Takeout zip and your existing analyses show
`watched_at: unknown`, you can populate that field from the zip's
watch-history:

```bash
python bin/backfill_watched_at.py --zip path/to/takeout-2024xxxxx.zip
```

Idempotent: only touches files where the front-matter is currently
empty or "unknown", so it never overrides a more-specific value.
Recent ingests via `pipeline.py` auto-populate `watched_at` from the
upstream `time` field — no manual backfill needed for new runs.

### What channels do I watch most?

```bash
python bin/takeout_sample.py --source takeout-watch-all --limit 41960 \
  | awk -F'|' '{print $5}' | sort | uniq -c | sort -rn | head -20
```

Outputs your top 20 channels by watch count. Real data from one Takeout
export today: top channels include `Dr. Scarry` (166), `Valuetainment` (144),
`Alex Hormozi` (143), `Vusi Thembekwayo` (128), `Varun Mayya` (98), `Aevy TV` (98).

## Project shape

```
video-pipeline/
├── bin/
│   ├── analyze.py            # 1-URL → 4-shape Markdown + SQLite row
│   ├── ask.py                # RAG over chosen/all transcripts (bundle-and-ask)
│   ├── chat.py               # multi-turn REPL with history persistence
│   ├── pipeline.py           # Takeout → sample → analyze; supports --resume
│   ├── url_source.py         # URL sources (takeout-watch, takeout-watch-all)
│   ├── vector_store.py       # chunk + embed + cosine search + chat persistence
│   ├── end_to_end_check.py   # one runnable check that the pipeline works
│   └── takeout_sample.py     # Compatibility alias of url_source.py
├── corpus -> ~/Documents/video-analysis   # symlink: outputs land here
│   ├── <slug>.md                            # 4-shape analysis per video
│   ├── <slug>.transcript.txt                # transcript sidecar
│   └── analyzed.sqlite                       # global index
├── docs/
│   ├── CONVENTIONS.md       # File format spec for corpus/*.md
│   ├── REQUIREMENTS.md      # What's built, what's deferred, all triggers
│   └── ANALYSIS-FALLBACK.md # 3-tier fallback design for transcripts
├── README.md (this file)
└── LICENSE                  # MIT
```

The 5 scripts in `bin/` are ~1000 LOC of Python total. Stdlib-only except
`google-genai` (for Gemini API calls) and `youtube-transcript-api` (for
caption extraction).

## Documentation

| Doc | What's in it |
|---|---|
| `docs/CONVENTIONS.md` | The Markdown front-matter schema. What fields every `<slug>.md` must have. |
| `docs/REQUIREMENTS.md` | Status of every feature: built, deferred, rejected. Each deferred item names its trigger. |
| `docs/ANALYSIS-FALLBACK.md` | Why shorts fail to analyze today. 3-tier fallback design (transcript → multimodal audio → local STT). |
| `docs/SEMANTIC-SEARCH.md` | Why `ask.py` doesn't do semantic search today, what it costs to add, and the trigger that earns the build. |

Read `REQUIREMENTS.md` first if you want to know what's *not* built and why.

## Status (as of 2026-07-12)

- 9 analyzed videos in the corpus; 40,030 unique YouTube URLs in Takeout
  waiting to be processed.
- Pipeline supports `--resume` with state file: walk the corpus across
  many runs at Gemini free-tier pace (50–200 text-mode calls/day).
- Vector store is **built**: `sqlite-vec` + `sentence-transformers/
  all-MiniLM-L6-v2` (384-dim). 75 chunks indexed across 3 transcripts;
  `ask.py --all` uses cosine top-k, `bin/chat.py` uses it per turn.
- Multi-turn chat works: `bin/chat.py` REPL with persisted history
  (8-message cap per turn).
- Classification works: `analyze.py --classify` tags each analyzed video
  from a fixed 9-tag vocabulary; `analyze.py --reclassify` re-tags existing
  files. `ask.py --tag <t>` filters retrieval to slugs matching `<t>`.

## Recent changes

The full history lives in `git log`. Most recent commits (newest first):

| commit | what |
|---|---|
| *(this turn)* | D3 + D4: classification (`analyze.py --classify` / `--reclassify`) + tag-filter (`ask.py --tag`); fixed `p.stem` bug in `--all` slug extraction |
| `ce42fcb` | End-to-end check script (`bin/end_to_end_check.py`) + README verify section |
| `8a595f8` | Multi-turn chat REPL (`bin/chat.py`) + chat-store functions in `bin/vector_store.py` |
| `2cb4de5` | README refresh: vector-store / show-chunks / chat rows |
| `92fde63` | `--show-chunks` flag in `ask.py` — print raw retrieved transcript excerpts |
| `db2cfb4` | Vector store flipped to built; `docs/SEMANTIC-SEARCH.md` retired |
| `49e2a7a` | Semantic search end-to-end: sqlite-vec + sentence-transformers |
| `0e0d2a8` | Deferred-rung doc for semantic search (later built) |
| `1d6cd62` | Initial commit: pipeline + RAG + Takeout ingest |

Run `git log --oneline` for the full list, or see
[commits on GitHub](https://github.com/azim12086atmes-s/YouTube-Knowledge-Base/commits/master).

## License

MIT. See `LICENSE`.
