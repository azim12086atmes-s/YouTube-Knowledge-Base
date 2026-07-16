# bin/ — script-writing area

This file is the **script-writing contract** for the `bin/`
directory. It is a child of the root `AGENTS.md`; both apply
to any file in `bin/`. Read both before editing.

## Purpose

`bin/` holds the executable scripts that make up the
`video-pipeline`. Each script is a self-contained CLI tool
that can be invoked from a shell, a cron, or another script.
There is no `bin/__init__.py` and no shared package — every
script is its own entry point. Scripts `import` from each
other freely (e.g. `web.py` imports `chat.py`); the
relationships are documented in this file and in
`docs/ARCHITECTURE.md`.

## Ownership

Every script in `bin/` is owned by the user. The agent
maintains them; the user reviews. Branch policy: each
non-trivial change lives on a `work/<feature-name>` branch
until the user approves the merge to `master`. See root
`AGENTS.md` for the full branch policy.

## Local Contracts

These rules apply to every script in `bin/`. Sub-areas (a
single script's `Local Contracts` section in its own
AGENTS.md, if one exists) may add more but must not weaken
these.

- **Every script is `python bin/<name>.py --help`-able.**
  The `--help` output is the script's contract with the
  outside world. The e2e_check suite probes every CLI's
  `--help` exits 0. If a new flag is added, it must show up
  in `--help` and in at least one e2e probe.
- **Idempotent re-runs.** Re-running a script with the same
  args must not duplicate or corrupt state. `bin/analyze.py`
  is keyed on the SQLite index; `bin/jobs.py enqueue` is
  keyed on `key_hash`; `bin/pipeline.py --resume` is keyed
  on the cursor file. New scripts follow the same
  pattern: write state to SQLite (or a SQLite-equivalent
  deterministic store) and read it on entry.
- **Exit codes are integers: 0 = success, 1 = skip (e.g.
  transcript disabled, junk caption, dry-run), 2 = error.**
  Scripts must not raise on a skip; they must `print(...)`
  to stderr and `return 1`. The e2e_check suite asserts
  on exit codes.
- **Logs go to stderr; results go to stdout.** This is the
  Unix convention; the e2e_check suite captures both
  streams separately and the bin's tests need both.
- **No global mutable state across script invocations.** A
  script's import-time side effects (other than a tiny
  lazy-load of models) are forbidden. The embedding model
  in `vector_store._model()` is a deliberate exception
  because loading it twice wastes ~5 seconds and ~80 MB;
  the singleton is documented at the call site.
- **Pinned deps only.** A new dep added to `requirements.txt`
  must come with a justification in the commit message
  (which pain does it address?) and a probe in
  `end_to_end_check.py` (D21: pin matches dev venv).

## Work Guidance

- **Read the script before editing.** Most bugs in this
  codebase are "the function does X, the caller assumes Y."
  Grep every caller of the function you're about to touch
  before patching. Ponytail rule: the lazy fix is the
  root-cause fix; one guard in the shared function is a
  smaller diff than a guard in every caller.
- **Add an e2e probe in the same commit as the change.**
  The probe pattern is `check(name, ok, detail)` and goes
  into `bin/end_to_end_check.py`. No per-function unit tests
  with mocks. The probe is a subcommand invocation that
  exits 0 on pass; e2e_check.py is the harness.
- **When a change adds a CLI flag, update `--help` and the
  README's Usage section in the same commit.** README
  drift is a real bug.
- **When a change adds a Web API endpoint, update
  `bin/web.py`'s route table in its docstring AND the
  README's Web UI table AND the e2e probe. Three places
  or it doesn't count as shipped.**
- **When a change adds a new D# to `docs/REQUIREMENTS.md`,
  the row must include the trigger that gates it. Trigger
  format: a sentence stating the condition under which the
  rung fires, e.g. "When the user has analyzed > 50
  multimodal-mode files and the disk usage of the corpus
  is > 5 GB." Triggers are the discipline that prevents
  speculative work.
- **Reuse before adding.** If a helper exists in any
  other `bin/*.py` and it does what you need, import it.
  The agent_skill_graphify wrapper (added in D30) is the
  canonical example: graphify is reachable from any script
  via `bin/agent_skill_graphify.py`; do not reimplement the
  graph build.

## Verification

`bin/end_to_end_check.py` is the canonical verifier. It
runs 60+ probes across:

- Every CLI tool's `--help` exits 0.
- Every Web API endpoint (live port + TestClient).
- Every retrieval mode (dense, hybrid, pinpoint).
- Every corpus state invariant (chunks == meta, FTS5
  backfill, FTS rows == meta rows, tag coverage).
- The requirements pin matches the dev venv (D21).
- The agent_loop surfaces unshipped D# rows.
- The agent_skill_graphify wrapper.

A wall time of ~2:30 is normal. If a probe fails, fix the
underlying code, do not weaken the probe.

## Child DOX Index

`bin/` has no child `AGENTS.md` files today. The scripts
themselves are not durable boundaries — they're a flat
namespace of CLI tools. If a script grows a *family* of
related tools (e.g. `bin/agent_skill_*` becomes a family),
create `bin/agent_skill/AGENTS.md` for the family and
update this index.

## Style

- One file, one purpose. If `analyze.py` is doing 8 things,
  it's a sign that 4 of them should be in `analyze.py` and
  4 of them should be elsewhere. Resist the urge to
  refactor on the way through a fix; file a follow-up D#
  instead.
- Functions get docstrings that start with a one-line
  summary, then expand. `bin/analyze.py` is the model.
- Comments are sentences. `ponytail: <ceiling>` is the
  project-wide marker for "this is a deliberate shortcut,
  here's the upgrade path."
- Imports: stdlib first, then third-party (alphabetical
  within each), then local. One import per line for
  non-`from` lines.

## End
