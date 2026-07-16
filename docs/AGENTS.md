# docs/ — doc-writing area

This file is the **doc-writing contract** for the `docs/`
directory. It is a child of the root `AGENTS.md`; both
apply to any file in `docs/`. Read both before editing.

## Purpose

`docs/` holds the durable project documentation that the
agent and the user both read at session start. There are
four files; their roles are fixed and named here so the
agent does not invent new docs without a D# in
`REQUIREMENTS.md` to back it.

- `REQUIREMENTS.md` — the source-of-truth ledger. Every
  feature the user has named lives here as a `| D# | ... |`
  row with a trigger that gates it. **If a feature is not
  in this table, it is not on the project.**
- `CONVENTIONS.md` — the file-format spec for the corpus.
  Markdown front-matter for `<slug>.md`; column format for
  the corpus inventory; output encoding for transcripts.
- `ARCHITECTURE.md` — the system design, side-by-side with
  NotebookLM, with a RAG-architecture subsection. Kept
  short — link to memo files in
  `~/Documents/notes/research/` for depth.
- `ANALYSIS-FALLBACK.md` — the multimodal-mode fallback
  (when YouTube has no captions and we send the video to
  Gemini directly). Single file, rarely edited.

## Ownership

`docs/` is owned by the agent under the user's review.
The merge to `master` waits for explicit "merge" /
"approved" / "go". See root `AGENTS.md` for branch policy.

## Local Contracts

- **`REQUIREMENTS.md` is append-only for shipped D#s.**
  Once a D# row is marked "✓ Shipped <date>", it is a
  historical record. Don't edit the row text; add a new
  "**Y-M-D update**" sub-sentence to the trigger column if
  something changes. Adding a new D# is fine; rewriting
  history is not.
- **D# trigger format is a sentence stating the condition
  under which the rung fires.** A trigger is not a TODO;
  it is a predicate. Example: "When the user has 200+
  ingested videos and wants to track a channel's new
  uploads." Predicate-shaped triggers are checkable
  before any work begins; TODO-shaped triggers are
  speculation.
- **Markdown front-matter for any new doc that
  describes a corpus state** (e.g. an analysis report)
  must follow `CONVENTIONS.md`. The fields are
  `title`, `channel`, `video_url`, `youtube_id`, `mode`
  (transcript | multimodal), `watched_at`, `analyzed_on`,
  `duration`, and any tags.
- **Every doc links to its source.** `docs/ARCHITECTURE.md`
  links to `docs/CONVENTIONS.md` and
  `~/Documents/notes/research/notebooklm-rag-architecture-2026-07-13.md`.
  `docs/REQUIREMENTS.md` links to `docs/ARCHITECTURE.md`
  and the commit hash for each D#. A doc that does not
  link to its sources is a doc that cannot be re-verified.

## Work Guidance

- **One file, one purpose.** If a doc starts to grow past
  500 lines, the content is *probably* two docs. Split.
- **Cite URLs and dates for every factual claim.** "71.5x
  token reduction (per Graphify-Labs/graphify README,
  2026-07-16)" beats "71.5x token reduction." A claim
  without a source is a hallucination seed; a claim
  with a source is a re-verifiable fact.
- **State "not publicly documented" or "ambiguous" when
  facts are missing.** Don't hedge; don't speculate. The
  e2e_check suite doesn't probe doc honesty (yet), but
  the user does.
- **Memo files (`~/Documents/notes/research/*.md`) are
  long-form; the README and the docs/ files are short-form
  summaries.** When the agent does research, save the
  long-form memo locally and link to it from the short-form
  README. Don't paste the full memo into the repo.
- **Update the README's project-shape tree when scripts
  are added/removed/moved.** A README that disagrees with
  `bin/` is a real bug.

## Verification

- **`bin/agent_loop.py --once` reads `REQUIREMENTS.md` and
  surfaces every unshipped D# row.** This is the closest
  thing to a "doc is current" probe: if a D# is in
  `REQUIREMENTS.md` but not in the e2e_check output, the
  parse is broken. E2E probe 11 covers this.
- **The ARCHITECTURE.md round-trip probe** (probe 7k)
  asserts that `docs/ARCHITECTURE.md` references each of
  the 5 NotebookLM features the project actually
  transfers. If a new feature transfers but ARCHITECTURE
  doesn't say so, the probe fails.
- **The requirements pin probe** (probe 7i) asserts
  `requirements.txt` matches the dev venv. Doc +
  runtime are coupled; this catches drift.

## Child DOX Index

`docs/` has no child `AGENTS.md` files today. The four
files in `docs/` are siblings, not a hierarchy. If a
doc grows a *family* of related sub-docs (e.g. a
`docs/rag/` directory with `chunking.md`,
`embedding.md`, `retrieval.md`), create
`docs/rag/AGENTS.md` for the family and update this
index.

## Style

- Headings are title-case. `## Section Name`, not
  `## Section name`.
- Lists are bulleted. Numbered lists only when order
  matters (steps, sequences).
- Code blocks are fenced with ` ``` ` and a language tag.
  Don't use indented blocks.
- Tables use the GFM pipe syntax. Don't use HTML.
- Doc length: 100-500 lines is the sweet spot. Below 100
  is a stub; above 500 is a candidate for splitting.
- Every doc has a "Last updated: YYYY-MM-DD" line near
  the top. The agent updates this on every meaningful
  edit.

## End
