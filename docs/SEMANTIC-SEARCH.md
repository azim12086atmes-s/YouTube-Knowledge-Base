# Semantic Search — when, why, and what it costs

Status: NOT BUILT. Documented for the day this becomes a real friction.

## What semantic search is

```
query → embed query (a 768-dim vector)
      → search vector store for top-k nearest neighbors
      → retrieve the matching chunks
      → stuff chunks into the LLM prompt
      → LLM answers
```

This is the standard RAG retrieval step. Our current `ask.py` skips it
and does *bundle-and-ask* instead — concatenates every transcript into
one prompt, hands it to Gemini.

## Why bundle-and-ask is enough today

`ask.py --all` reads every `<slug>.transcript.txt` in the corpus, joins
them, and POSTs the bundle to `gemini-3.1-flash-lite` (text mode).

- Gemini text input cap: **~60 KB per request** (analyze.py uses this
  threshold; ask.py uses the same).
- Current corpus size: **~38 KB** (3 transcripts with real speech).
- Failure mode: when corpus > 60 KB, the prompt gets truncated and the
  model sees a partial bundle. *It will still answer, but parts of the
  corpus are invisible to it.* That's the failure pattern that earns
  semantic search.

## The ceiling

You don't need semantic search until one of:

1. **Corpus size > 200 KB.** Roughly 30+ transcripts. Bundle starts
   losing chunks. Quality of "ask across the corpus" answers degrades
   silently — the model doesn't know it's missing content.
2. **A real question fails.** User asks something specific and gets an
   answer that misses obvious relevant content. That's the signal that
   *retrieval matters*, not just "give the model everything."
3. **Latency becomes a problem.** Bundle-and-ask sends 60 KB on every
   call. With semantic search you send ~2 KB (top-k chunks). Faster,
   cheaper, scales.

Today none of these have fired.

## What semantic search costs

If/when you build it:

| Component | Cost | Notes |
|---|---|---|
| `sentence-transformers` | +200 MB disk (model) | English-only small = ~250 MB. CPU OK; GPU ~5× faster. |
| `numpy` | already pulled by some deps | |
| Vector store — options: | | |
| ↳ SQLite + sqlite-vec | 0 disk, 1 dep | Embeds stored in same `analyzed.sqlite` we already have. Best fit for our scale. |
| ↳ Chroma | +docker / persistent process | Over-kill at our scale. |
| ↳ FAISS in-memory | 0 disk, but loses on restart | Acceptable for a one-shot CLI, not for a long-running app. |
| Embedding compute | ~50 ms per transcript on CPU | Embed at analyze-time (write path), not at ask-time (read path). |
| Code change | ~80 LOC in `analyze.py` (write-side embed) + ~50 LOC in `ask.py` (read-side query) | Total ~130 LOC. |

Total: +250 MB disk, 0 new services, ~130 LOC.

## Design

### Write side (in `analyze.py`)

After writing `<slug>.md` and `<slug>.transcript.txt`:

```
1. Read the transcript text.
2. Chunk it (e.g. 512 tokens with 64-token overlap).
3. Embed each chunk with sentence-transformers/all-MiniLM-L6-v2
   (384-dim, fast, good for English).
4. Store (slug, chunk_idx, text, embedding) in `analyzed.sqlite`
   using the sqlite-vec extension.
```

### Read side (in `ask.py`)

When `--all` is asked:

```
1. Embed the user's question with the same model.
2. Query sqlite-vec for top-k=10 chunks nearest to the question.
3. Bundle those 10 chunks into the LLM prompt.
4. Gemini answers.
```

Two clear wins over bundle-and-ask:

- Latency: 10 chunks × ~200 tokens = ~2 KB to Gemini, not 60 KB.
- Quality: Gemini sees *relevant* content, not "everything we have
  hoping something's relevant."

### Trigger to install and build

When any of:

- Corpus size > 200 KB.
- A real question fails (you notice the answer misses obvious things).
- You start running `ask.py --all` daily and the latency bothers you.

Until then: bundle-and-ask is fine. Don't pre-build.

## What to install, when

```bash
uv pip install sentence-transformers sqlite-vec numpy
# First run downloads the embedding model (~80 MB).
```

Then ~130 LOC of glue.

## Why NOT to do this today

1. The corpus is 38 KB. Bundle-and-ask fits in one prompt.
2. No question has failed.
3. Latency is fine (one roundtrip to Gemini, ~5 s).
4. 250 MB disk + 130 LOC is real cost for zero observable benefit.

The lazy path is: wait. The cost is documented here. When the trigger
fires, you've already done the design.
