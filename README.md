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
* Not a multi-turn chat UI. `ask.py` is one-shot Q&A over the corpus;
  there's no conversation memory or chat-style interface.

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
| Chat interface (multi-turn, conversation memory, UI) | ✗ — gated on a real question that needs back-and-forth; today `ask.py --question "..."` is single-shot |
| Query raw transcript chunks verbatim | ✓ — `ask.py --show-chunks` prints top-k retrieved excerpts with slug + cosine distance before the LLM answer |
| Vector store / semantic search | ✓ — `sqlite-vec` + `sentence-transformers/all-MiniLM-L6-v2` (384-dim). `ask.py --all` uses cosine top-k; explicit-URL mode falls back to bundle-and-ask when the index has no embeddings. |
| Continuous extraction daemon (poll every 20 min) | ✗ — gated on a continuous input source |
| Push trigger from YouTube webhooks | ✗ — gated on a real external process |
| Headless-browser auto-fetch of similar videos | ✗ — rung-1 reject (YouTube ToS) |

## Install / setup

```bash
# 1. Clone
git clone <repo-url> video-pipeline
cd video-pipeline

# 2. Python deps — stdlib + google-genai + youtube-transcript-api
#    + sentence-transformers + sqlite-vec (for vector search, ~250 MB on disk)
pip install google-genai youtube-transcript-api sentence-transformers sqlite-vec

# 3. API key
echo 'GEMINI_API_KEY=YOUR_KEY_HERE' >> ~/.hermes/.env
# Get a key at: https://aistudio.google.com/apikey

# 4. (Optional) Drop a Google Takeout zip in ~/Downloads/
#    go to https://takeout.google.com → YouTube → history → JSON format

# 5. (Optional) Backfill embeddings from existing transcripts:
#    python bin/analyze.py --reindex-from-md
```

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

To see the *raw* retrieved transcript excerpts (slug + cosine distance +
verbatim text) before the LLM answer:

```bash
python bin/ask.py --all --question "conscience and war" --show-chunks
```

Useful when the LLM-rendered answer loses detail that the raw chunk
preserves, or when you want to quote a video directly without going
through the model.

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
│   ├── analyze.py       # 1-URL → 4-shape Markdown + SQLite row
│   ├── ask.py           # RAG over chosen/all transcripts (bundle-and-ask)
│   ├── pipeline.py      # Takeout → sample → analyze; supports --resume
│   ├── url_source.py    # URL sources (takeout-watch, takeout-watch-all)
│   └── takeout_sample.py# Compatibility alias of url_source.py
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

- 9 analyzed videos across this corpus; 40,030 unique YouTube URLs in
  Takeout waiting to be processed.
- The pipeline works for batches up to ~50 with current scripts; cross-day
  resume is real.
- The corpus is too small to need embeddings. Bundle-and-ask handles 38 KB
  today; vector store becomes worth building at ~60 KB of corpus or when a
  real question fails.
- Free-tier Gemini quota is the binding constraint on processing speed
  (50–200 text-mode calls/day).

## License

MIT. See `LICENSE`.
