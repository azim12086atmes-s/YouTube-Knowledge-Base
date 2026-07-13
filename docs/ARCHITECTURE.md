# Architecture — how this pipeline compares to NotebookLM

This doc is **not a feature spec.** It is a short, explicit mapping between
the pieces of `video-pipeline` and the pieces of Google NotebookLM, so
the next maintainer (you, future-me) understands why the design looks
like this. Last reviewed 2026-07-13.

The summary: **this pipeline is architecturally a NotebookLM-shaped
system** for a single local corpus. Most of the ideas you'd borrow from
NotebookLM we already have; most of the things NotebookLM has that we
don't, we *deliberately* don't, because we're single-user + local-first.

## Side-by-side

| NotebookLM (public docs)                          | video-pipeline                                              | Status |
|---|---|---|
| Sources: PDFs, URLs, Google Docs, audio            | Sources: Takeout zip JSON, xlsx, txt, jsonl URL lists        | partial — same shape, fewer formats |
| Per-source cap: 500,000 words / 200 MB             | One URL → one markdown; quota is the cap                    | different axis |
| Per-notebook source cap: 50–600                    | One corpus, all sources indexed                             | simpler (one-corpus, no source cap) |
| Chunking: not publicly disclosed                  | 120-word windows, 20-word overlap (see `vector_store.CHUNK_WORDS`) | documented |
| Embedding model: not publicly disclosed            | `sentence-transformers/all-MiniLM-L6-v2` (384-dim)          | disclosed |
| Distance metric: not publicly disclosed            | L2 on unit-norm embeddings (= cosine rank)                  | disclosed |
| Top-k retrieval: not publicly disclosed            | 8 chunks for chat, 10 for ask                                | disclosed |
| Reranker: not publicly disclosed                   | None                                                         | held |
| LLM: Gemini 2.5 Flash (officially named 2025-05-02) | `gemini-3.1-flash-lite` for ask/chat/web, `gemini-flash-lite-latest` for text-only four-shape | different models, same family |
| Inline citations with verbatim quotes              | Prompt asks model to "quote (slug + verbatim phrase)"; `--show-chunks` exposes raw excerpt + cosine distance | works |
| Click-through to source location                  | Not implemented — would require transcript timestamps re-ingest | rung-1 hold |
| Source selection per query (include/exclude)      | Per-query `--tag` filter + per-session REPL tag; no per-source include/exclude by file | partial |
| Honest refusal on no-evidence                      | Prompt forces "say so explicitly if the transcripts don't address"; bundle-and-ask fallback | works |
| Audio Overview                                     | Not implemented — see rung-1 hold below                       | rung-1 hold |

The big thing NotebookLM has that we don't: **per-chunk timestamp
anchors.** Their citations can point to a specific second in a 20-min
video. We *could* add this — `youtube-transcript-api` exposes segments
with start/duration via `to_dict()` instead of `--text-only`. Doing so
costs ~50 LOC + re-ingestion quota. Adding it without a measured
*"I need to point someone at minute 17"* use case would be YAGNI.

The big thing NotebookLM has that **we correctly reject**: an integrated
audio-overview feature and a hosted multi-tenant service. Both run on
Google's paid server-side infrastructure and aren't single-user-local
first — explicitly out of scope per `docs/REQUIREMENTS.md` "What this
is *not*."

## Why our design is more conservative than NotebookLM

1. **Local-first**: every script runs on the operator's machine. No
   server costs, no per-call metering beyond the actual Gemini API.
   Trade-off: free-tier quota becomes the binding constraint, not
   compute.
2. **One corpus, one DB**: `analyzed.sqlite` carries every source. The
   schema is shared between the indexing write path (`analyze.py` /
   `--reindex-from-md`) and the retrieval read path (`ask.py` /
   `chat.py` / `web.py`). Adding a "second notebook" requires either a
   schema-keyed multi-corpus split (held at rung-1) or running a
   second instance on a different port.
3. **Stdlib over deps**: openpyxl is rejected in favor of zipfile +
   ElementTree; only 4 Python packages are pinned in `requirements.txt`
   (+ 2 more for the web UI). Single-corpus debugging is fast.
4. **Prompts are explicit**: every retrieval prompt carries an "if no
   evidence, say so" guard and a "quote (slug + phrase)" directive.
   This is the cheapest anti-hallucination lever available without
   fine-tuning.

## What is **not** in this pipeline and *should stay out*

- A second embedding model. The 384-dim nomic alternative would gain
  marginal quality for marginal latency cost — not worth an eval-and-
  migrate unless retrieval fails measurably. (Today the biggest
  failure mode for the corpus is short, low-signal transcripts, not
  embedding quality.)
- A reranker. Same reasoning. Add when measured miss rate on
  `--show-chunks` becomes a complaint.
- Hybrid retrieval (BM25 + dense). Memo explicitly says NotebookLM
  doesn't publish a hybrid setup, so we have nothing to copy. Add
  when retrieval can't find paraphrased matches.
- Hosted user service. Local-first is the contract.

See `docs/REQUIREMENTS.md` for the full deferred-rungs table.
