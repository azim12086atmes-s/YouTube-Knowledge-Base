# Video Analysis — File Convention

This folder holds one **markdown file per analyzed video**. It lives in `~/Documents/video-analysis/` so Obsidian surfaces it automatically.

## Filename

`<youtube_id>.md` — bare 11-char YouTube ID, no prefix. Examples:
- `M1E4ZzdpOco.md`
- `dQw4w9WgXcQ.md`

If multiple analyses exist for the same ID (e.g. A/B comparison + later deeper dive), suffix with `.<n>`. Example: `M1E4ZzdpOco.2.md`.

## Front-matter (YAML)

```yaml
---
youtube_id: <id>
url: https://www.youtube.com/watch?v=<id>
title: <as it appears on the video or your own phrase>
speaker: <host / channel>           # optional
analyzed_on: <YYYY-MM-DD>
analyzed_with: <model id>           # e.g. gemini-3.1-flash-lite, m3
cross_checked_with: <model id>      # optional, A/B runs
duration: <approx minutes>          # optional
model_validation: <notes>           # e.g. "gemini-2.5-flash deprecated, 3.1-flash-lite used"
tags: []                            # empty until classification layer activates
---
```

## Body sections (4 shapes)

Every file uses exactly these four:

1. **`## 1. Summary`** — 3-5 sentence gist + main argument + subtopics list with timestamps
2. **`## 2. Key Takeaways`** — 7-10 numbered items; each has bold headline, 2-3 sentence explanation, timestamps, "why it matters" line
3. **`## 3. Non-Obvious Insights`** — 5-8 items; sharp sentence + why non-obvious + timestamps + "extends to" line
4. **`## 4. Revolutionary Reframes`** — 3-5 items; reframe + old assumption + timestamps + skeptical pushback

End the file with a `## Provenance & notes` section: model versions, A/B status, transcript-length gotchas, anything you want the next reader to know.

## Searching this folder

Without RAG (rung-1 today):
```
grep -r "^## 1\." *.md | head     # list all summary sections
rg -i "vibe.cod" *.md              # full-text grep across all analyses
```

With Obsidian: just search the vault. Each `.md` shows up as a note + graph node automatically because of the front-matter's `youtube_id` and the body text.

## When to extend the convention

- **Adding a tag column?** Update front-matter `tags: []` to your category list. Don't reshape existing files until you have 10+ tagged.
- **Adding embeddings?** Add a sister `.embedding.json` next to each file (e.g. `M1E4ZzdpOco.embedding.json`). Don't move the canonical markdown.
- **Adding per-section timestamps?** If a section gets rich enough, break out a `<shape>.json` companion. Markdown stays canonical; JSON is secondary.

This file is the source of truth for format. Update it when you change the convention; everything else follows.
