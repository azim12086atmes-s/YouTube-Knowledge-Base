# Video Analysis Fallback Architecture

Why: today, `analyze.py` skips ~30–50% of YouTube URLs it tries because
transcripts are missing, thin, or junk. This doc is the design; nothing
is built until a rung fires.

## Failure modes observed today (2026-07-10)

| # | Cause | Symptom | Pipeline today |
|---|---|---|---|
| 1 | No auto-captions on YouTube side | `{"error": "No transcript found..."}` | Skip-write |
| 2 | Captions disabled by uploader | `{"error": "Transcripts are disabled..."}` | Skip-write |
| 3 | Captions exist but tiny (Shorts, ads, music) | `len < 200 chars`, often `[Music]` loop only | Skip-write (threshold) |
| 4 | Captions exist but junk (noisy audio, music overlay) | Transcript > 200 chars but content is `[Music]` repeated, `[__]` mumbles, or hallucinated | Writes a low-quality summary — current threshold too lenient |
| 5 | Region-blocked or brand-protection Gemini block | HTTP 403 PERMISSION_DENIED on multimodal | Skip-write |
| 6 | Free-tier Gemini multimodal quota exhausted | HTTP 429 | Skip-write (after 3 retries) |

## Three-tier fallback design

Each tier is wider-net and heavier-cost than the one above:

### Tier 1 — `youtube-transcript-api` (current default; covers ~95% of cases)

- Cost: free, instant, no install beyond what's already present.
- Failure handling: today, returns non-zero exit code or empty stdout.
- **Quality improvement needed** (cheap, ~10 LOC):
  - Reject transcripts where `> 50%` of lines are `[Music]`, `[Applause]`,
    `[__]`, or empty (whitespace-only). `len > 200` is a necessary,
    not sufficient, signal.
  - Reject transcripts where the *unique-word count / total-word count*
    ratio is below 0.3 (junk captions repeat the same 1–2 lines).

### Tier 2 — Local `faster-whisper` audio transcription

- Cost: ~500 MB model file on disk; ~1 GB RAM during inference; pip-install
  `faster-whisper` (50 KB code, but pulls in `transformers` family).
- Trigger: only when Tier 1 fails AND user passes `--allow-fallback`.
- Steps: `yt-dlp -x --audio-format wav <url>` → `faster-whisper` on the WAV →
  inject text into `call_gemini_text(...)` (already works).
- **Honest tradeoff**: 1 GB RAM baseline is non-trivial for a single-shot
  fallback. Only worth it if you're analyzing video at scale AND you don't
  want to spend money. Otherwise prefer Tier 3.

### Tier 3 — Gemini multimodal audio-only (currently mis-shelved, easy to fix)

This is the path your `gemini-video-understanding` skill *already* supports.
Today we use the same endpoint for the 4-shape prompts (which transcribes
audio incidentally — the multimodal response IS a transcription). The
folded cost is rate-limiting across the WHOLE pipeline. The unlocked
opportunity: **a separate "transcribe only" prompt** sent through
multimodal that just returns the audio transcript, decoupled from the
4-shape output.

- Prompt for Tier 3:
  ```
  Transcribe the spoken audio of this YouTube video.
  Return ONLY the verbatim transcript with timestamps like
  [mm:ss] speaker: text. Do not summarize, do not analyze.
  If the audio contains non-speech (music, silence), say so
  and skip.
  ```
- Cost: 1 Gemini multimodal call vs the 4 we do today. Fits the
  free-tier quota when used sparingly.
- Quick win: when `analyze.py --transcript` mode can't fetch a transcript,
  retry via Tier 3 once, then skip-write. ~30 LOC, no new deps.

### Tier 4 — Skip-write (last resort)

- Always supported. Document reason in stderr.
- The `---` separator makes it clear in stdout which URL was skipped.

## Recommended activation order

1. **Tier 1 quality** (10 LOC, in `analyze.py`, no install).
   Trigger: today. I have a 2026-07-10 observed failure where the
   transcript was 75% `[Music]` and analyze.py wrote a useless summary.
2. **Tier 3 integration** (30 LOC, in `analyze.py`, no install).
   Trigger: when at least one video fails Tier 1 *and* a real concrete
   failure shows up that Tier 3 would fix. Until then, deferred.
3. **Tier 2 (faster-whisper)** (~30 LOC + 500 MB model file + `yt-dlp`).
   Trigger: when offline-only requirement emerges (e.g. during a flight
   or a no-network sprint week). Until then, Tier 3 covers it.
4. **Skip-write hardening**: when a Tier-1+3 path keeps failing for
   a *category* of URLs (e.g. all Shorts), document the failure mode in
   `REQUIREMENTS.md` and skip silently. Trigger: when overall skip rate
   exceeds ~30% over a 30-video sample.

## Ponytail notes

- Tier 3 is the cheapest rung we haven't built. Today it's `analyze.py` does
  multimodal only when `--multimodal` is passed. Adding Tier-3 as a *fallback*
  (not a primary mode) doesn't flip the default or burn extra quota in the
  happy path.
- Tier 2 needs hard thinking: do you really want a 500 MB model file living
  on your laptop? Most cases Tier 3 is fast enough. Don't pre-build.
- The "all videos analyzable" target is reached when Tier 1 + Tier 3 cover
  ~98% of YouTube URLs. Tier 2 covers the remaining 2%. Tier 4 is for the
  rest (deleted videos, region-blocked, etc.) which no pipeline can fix.

## What this doc IS NOT

- Not a build spec. Don't wire any of these tiers until a concrete failure
  triggers a tier activation.
- Not a "do everything now" plan. Each tier is gated by observations.
- Not a measurement tool. We don't have a corpus-wide skip-rate yet; we
  have a 7-file sample (~14% skip rate from this session, all on the
  hm2xn/zjplv-style Shorts).
