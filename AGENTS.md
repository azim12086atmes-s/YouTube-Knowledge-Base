# video-pipeline — DOX rail

This file is the **DOX rail** for the `video-pipeline` project.
Adapted from [agent0ai/dox](https://github.com/agent0ai/dox) (MIT,
1,276★ as of 2026-07-16). See `bin/AGENTS.md` and
`docs/AGENTS.md` for local contracts in the script-writing and
doc-writing areas.

## Core Contract

- AGENTS.md files are binding work contracts for their subtrees.
- The closest `AGENTS.md` to a file plus every parent above it
  is the binding context for editing that file.
- **One agent per session** — single-operator project. The
  discipline is writing for an *unknown future agent* (you
  in a fresh session). That's the whole point of DOX.

## Read Before Editing

1. Read the root `AGENTS.md` (this file).
2. Identify every file or folder you expect to touch.
3. Walk from the repository root to each target path.
4. Read every `AGENTS.md` found along each route.
5. If a parent lists a child whose scope contains the path,
   read that child and continue from there.
6. Use the nearest `AGENTS.md` as the local contract and
   parent docs for repo-wide rules.
7. If docs conflict, the closer doc controls local work
   details, but no child doc may weaken project-wide rules.

**Do not rely on memory. Re-read the applicable DOX chain in
the current session before editing.**

## Update After Editing

Every meaningful change requires a DOX pass before the task
is done. Update the closest owning `AGENTS.md` when a change
affects:

- purpose, scope, ownership, or responsibilities
- durable structure, contracts, workflows, or operating rules
- inputs, outputs, permissions, constraints, side effects, or
  artifacts
- AGENTS.md creation, deletion, move, rename, or index
  contents

Update parent docs when parent-level structure, ownership,
workflow, or child index changes. Update child docs when
parent changes alter local rules. Remove stale or
contradictory text immediately. Small edits that do not change
behavior or contracts may leave docs unchanged, but the DOX
pass still must happen.

## Project-wide Rules

- **Single-operator, local-first.** No auth, no quotas, no
  multi-tenant. The single user is `karee` on Windows +
  git-bash + Hermes venv at
  `~/AppData/Local/hermes/hermes-agent/venv/`.
- **Python 3.11.** Dev venv python is
  `~/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe`.
- **Stdlib first.** New code should not pull in a dep for
  what `sqlite3`, `subprocess`, `pathlib`, `urllib`, `json`,
  or `csv` can do. Bump `requirements.txt` only when no
  stdlib path exists.
- **No silent abstractions.** If a helper has only one
  caller, do not extract. Two callers → extract; the e2e
  suite verifies the new surface.
- **Every non-trivial logic leaves a runnable check.** New
  branches, parsers, state transitions add a probe to
  `bin/end_to_end_check.py` in the same commit. Pattern:
  one `check(name, ok, detail)` line. No per-function unit
  tests, no frameworks, no fixtures.
- **Working branches per feature.** Anything ≥ 50 LOC, schema
  changes, doc rewrites, or new scripts goes on a branch
  `work/<feature-name>`, pushed but NOT merged. Trivial
  one-line fixes (typos, comment fixes) can still go to
  `master` directly. The merge to `master` waits for the
  user's explicit "merge" / "approved" / "go". This rule
  is enforced by the user; the agent's job is to follow it
  without being re-prompted.
- **Markdown body for the corpus, SQLite for the index.**
  Videos are analyzed into `<slug>.md` at
  `~/Documents/video-analysis/`. The same dir holds
  `analyzed.sqlite` (the index) and `.transcript.txt` /
  `.transcript.jsonl` (per-slug sidecars). The repo's
  `corpus/` is a symlink to that path; do not commit
  corpus files.
- **DOX is a discipline, not a tool.** The repo contains
  `AGENTS.md` files (this one + `bin/AGENTS.md` +
  `docs/AGENTS.md`) and that's the whole integration. The
  `agent0ai/dox` repo itself is just a 3.9 KB template +
  a README. No package, no runtime, no daemon.

## Verification

- The canonical end-to-end check is
  `bin/end_to_end_check.py`. 60+ probes across every CLI
  tool's `--help`, every Web API endpoint, every retrieval
  mode, every corpus state invariant, the requirements pin
  match, the agent_loop's unshipped-D# list, the
  agent_skill_graphify wrapper. Run it after any change;
  ~2:30 wall is normal.
- The DOX discipline is verified by *the agent reading this
  file before editing* — a behavioral rule, not a probe.
- `bin/agent_loop.py --once` surfaces the unshipped D# rows
  from `docs/REQUIREMENTS.md`. Use it at session start.

## Child DOX Index

- `bin/AGENTS.md` — script-writing area. CLI conventions,
  `--help` requirement, e2e probe pattern, exit-code
  contract, logging convention, the `_gemini.py` shared
  helper, the `vector_store.*` API surface, the
  `jobs.sqlite` schema, the `web.py` HTTP routes, the
  agent_loop cadence, the `agent_skill_graphify.py`
  wrapper. Read this before editing any file in `bin/`.
- `docs/AGENTS.md` — doc-writing area. Markdown front-matter
  conventions, the D#-table format in `REQUIREMENTS.md`,
  the side-by-side comparison format in `ARCHITECTURE.md`,
  the RAG-investigation memo format at
  `~/Documents/notes/research/*.md`. Read this before
  editing any file in `docs/`.
- `~/Documents/video-analysis/` is the corpus. **Not in
  the repo.** The `corpus/` symlink points to it. Don't
  write to the corpus from this repo's automation without
  the user explicitly asking.

## Style

- Keep this file concise, current, and operational. If a
  rule applies only to a sub-area, move it to that area's
  AGENTS.md.
- Document stable contracts, not diary entries.
- Prefer direct bullets with explicit names. "The corpus
  is in `~/Documents/video-analysis/`" beats "the corpus
  is somewhere."
- Delete stale notes instead of explaining history.
- Trim obvious statements, repeated rules, misplaced
  detail, and warnings for risks that no longer exist.

## Closeout

After any meaningful change:

1. Re-check changed paths against the DOX chain.
2. Update nearest owning docs and any affected parents or
   children.
3. Refresh every affected Child DOX Index.
4. Remove stale or contradictory text.
5. Run `bin/end_to_end_check.py` if the change touches
   code.
6. Report any docs intentionally left unchanged and why.

## End
