"""web.py — FastAPI HTTP wrapper around chat.py + vector_store.

ponytail: every query path is one HTTP route that calls one chat.py
function. No new RAG, no new persistence, no new model — chat.py +
vector_store are the source of truth. This file is plumbing.

Routes:
  GET  /                       serves the static chat UI (single index.html)
  POST /api/query              {question, session_id?, k?, tag?} -> {reply, chunks, used}
  GET  /api/sessions           list chat sessions
  GET  /api/sessions/{id}      session history
  DELETE /api/sessions/{id}    clear one session's history + tag state
  POST /api/sessions/{id}/tag  {tag} -> set/clear active tag filter
  GET  /healthz                liveness

Run:
  python bin/web.py --port 8080

Requires: fastapi + uvicorn (already pinned in requirements.txt).
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ponytail: chat.py + vector_store are the source of truth; web.py imports
# their public surface without duplicating logic.
# ponytail: chat.py owns session_kv + retrieve + build_contents + call_gemini;
# vector_store owns chat_messages CRUD + tag CRUD.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _gemini import gemini_key  # noqa: E402
import chat as _chat                # session_kv, retrieve_chunks, build_contents, call_gemini
import vector_store as _vs          # tag vocab, get_slugs_by_tag, init_tags, chat_messages

DEFAULT_TRANSCRIPT_DIR = Path.home() / "Documents" / "video-analysis"
DEFAULT_PORT = 8080

app = FastAPI(title="video-pipeline web", version="0.1.0",
              docs_url=None, redoc_url=None)


class Query(BaseModel):
    question: str
    session_id: str = "default"
    k: int = 8
    tag: Optional[str] = None  # one tag per request — match ask.py --tag
    # ponytail: when `slugs` is set, the request bypasses vector search
    # and bundles the chosen transcripts directly. Use this for
    # "pinpoint precision across the videos I picked" — the user
    # already knows which sources they want.
    slugs: Optional[list[str]] = None
    # ponytail: per-slug char cap to prevent a single 1h transcript
    # (~6k words ≈ 30k chars) from drowning the prompt. ask.py uses
    # 60_000 per slug; we expose that knob so the UI can tune it.
    per_slug_chars: int = 60_000


class TagSet(BaseModel):
    tag: Optional[str]  # None clears


def _idx_path() -> Path:
    return DEFAULT_TRANSCRIPT_DIR / "analyzed.sqlite"


def _open_idx():
    """Lazy-open the corpus index. Used by every retrieval route."""
    p = _idx_path()
    if not p.exists():
        raise HTTPException(503, f"index not found at {p}; run analyze first")
    try:
        import sqlite_vec
        conn = sqlite3.connect(str(p))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        _vs.init_tags(conn)
        _chat.init_session_state(conn)
        return conn
    except Exception as e:
        raise HTTPException(500, f"index open failed: {e}")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/api/query")
def api_query(q: Query) -> dict:
    if not q.question.strip():
        raise HTTPException(400, "empty question")
    conn = _open_idx()
    try:
        # ponytail: tag filter from request overrides any session tag (REPL
        # behavior is per-session; web is per-request — different UX model).
        allowed = None
        if q.tag:
            if q.tag not in _vs.TAG_VOCAB:
                raise HTTPException(400, f"unknown tag {q.tag!r}")
            allowed = _vs.get_slugs_by_tag(conn, q.tag)

        # ponytail: dispatch — explicit slugs (URL-list mode) vs. vector
        # search (corpus-wide). ask.py has the same two paths. We reuse
        # ask.build_prompt + ask.load_transcripts so the prompt contract
        # is identical to the CLI tool the README documents.
        if q.slugs:
            import ask as _ask
            transcripts, missing = _ask.load_transcripts(q.slugs, _idx_path().parent)
            for m in missing:
                # log but don't fail — multimodal-mode analyses don't
                # have a sidecar; the prompt will just lack them.
                pass
            if not transcripts:
                raise HTTPException(
                    404,
                    f"no transcripts found for any of slugs={q.slugs}",
                )
            # ponytail: per_slug_chars cap. ask.build_prompt truncates
            # to TRANSCRIPT_BUDGET (60k) per source; we expose the same
            # knob with a smaller default so 6 long transcripts stay
            # within a single Gemini-3.1-flash context.
            truncated = []
            for t in transcripts:
                truncated.append(t if len(t) <= q.per_slug_chars
                                   else (t[:q.per_slug_chars]
                                         + f"\n\n[transcript truncated at {q.per_slug_chars} chars]"))
            transcripts = truncated
            prompt = _ask.build_prompt(q.question, transcripts, q.slugs)
            api_key = gemini_key()
            reply = _ask._post_text(prompt, api_key, _ask.GEMINI_MODEL)
            if reply.startswith("ERROR"):
                raise HTTPException(502, reply)
            _vs.save_message(conn, q.session_id, "user", q.question)
            _vs.save_message(conn, q.session_id, "model", reply)
            return {
                "session_id": q.session_id,
                "mode": "url-list",
                "reply": reply,
                "slugs_used": q.slugs,
                "missing_slugs": missing,
                "chunks": [],  # URL-list mode returns full transcripts, not chunks
            }

        # Default: vector-search across corpus (or filtered by tag).
        hits = _chat.retrieve_chunks(_idx_path(), q.question, k=q.k,
                                     allowed_slugs=allowed)
        history = _vs.load_messages(conn, q.session_id, limit=_chat.HISTORY_CAP)
        contents = _chat.build_contents(history, q.question, hits)
        api_key = gemini_key()
        reply = _chat.call_gemini(api_key, contents)
        if reply.startswith("ERROR"):
            raise HTTPException(502, reply)
        _vs.save_message(conn, q.session_id, "user", q.question)
        _vs.save_message(conn, q.session_id, "model", reply)
        return {
            "session_id": q.session_id,
            "mode": "vector",
            "reply": reply,
            "chunks": [
                {"slug": h["slug"], "distance": h["distance"],
                 "text": h["text"][:1500]}  # truncate for payload sanity
                for h in hits
            ],
        }
    finally:
        conn.close()


@app.get("/api/sessions")
def api_sessions() -> dict:
    conn = _open_idx()
    try:
        return {"sessions": _vs.list_sessions(conn)}
    finally:
        conn.close()


# ponytail: catalog endpoint so the URL picker UI can render a list
# of available videos. Filters by outcome/mode/tag like bin/list.py
# but returns JSON instead of pretty-printed columns.
@app.get("/api/videos")
def api_videos(
    tag: Optional[str] = None,
    mode: Optional[str] = None,
    outcome: Optional[str] = None,
    limit: int = 200,
) -> dict:
    conn = _open_idx()
    try:
        where, params = [], []
        if mode:
            where.append("v.mode = ?"); params.append(mode)
        if outcome:
            where.append("v.outcome = ?"); params.append(outcome)
        if tag:
            where.append(
                "v.slug IN (SELECT slug FROM tag_assignments WHERE tag = ?)"
            )
            params.append(tag)
        sql = ("SELECT v.slug, v.url, v.mode, v.outcome, v.analyzed_on "
               "FROM analyzed_videos v")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY v.analyzed_on DESC, v.slug LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        # ponytail: enrich with tags so the UI can show them inline.
        tags_by_slug: dict[str, list[str]] = {}
        if rows:
            placeholders = ",".join("?" for _ in rows)
            for s, t in conn.execute(
                f"SELECT slug, tag FROM tag_assignments "
                f"WHERE slug IN ({placeholders}) ORDER BY slug, tag",
                [r[0] for r in rows],
            ):
                tags_by_slug.setdefault(s, []).append(t)
        return {
            "videos": [
                {
                    "slug": r[0],
                    "url": r[1],
                    "mode": r[2],
                    "outcome": r[3],
                    "analyzed_on": r[4],
                    "tags": tags_by_slug.get(r[0], []),
                    # ponytail: has_transcript tells the picker whether
                    # this slug can be bundled (URL-list mode requires
                    # a transcript sidecar; multimodal-only slugs
                    # still work but lose fidelity).
                    "has_transcript": (DEFAULT_TRANSCRIPT_DIR
                                       / f"{r[0]}.transcript.txt").exists(),
                }
                for r in rows
            ],
        }
    finally:
        conn.close()


# ponytail: transcript preview. The URL-picker UI needs to show a
# snippet so the user knows what they're picking. 800 chars is enough
# to disambiguate without burning API calls.
@app.get("/api/transcripts/{slug}")
def api_transcript(slug: str, chars: int = 800) -> dict:
    p = DEFAULT_TRANSCRIPT_DIR / f"{slug}.transcript.txt"
    if not p.exists():
        raise HTTPException(404, f"no transcript for {slug}")
    text = p.read_text(encoding="utf-8", errors="replace")
    return {
        "slug": slug,
        "preview": text[:chars],
        "total_chars": len(text),
        "truncated": len(text) > chars,
    }


@app.get("/api/sessions/{session_id}")
def api_session_history(session_id: str) -> dict:
    conn = _open_idx()
    try:
        msgs = _vs.load_messages(conn, session_id, limit=10_000)
        return {"session_id": session_id, "messages": msgs,
                "active_tag": _chat.get_session_kv(conn, session_id, "tag")}
    finally:
        conn.close()


@app.delete("/api/sessions/{session_id}")
def api_session_delete(session_id: str) -> dict:
    conn = _open_idx()
    try:
        cleared = _vs.clear_session(conn, session_id)
        _chat.set_session_kv(conn, session_id, "tag", None)
        return {"session_id": session_id, "cleared_messages": cleared}
    finally:
        conn.close()


@app.post("/api/sessions/{session_id}/tag")
def api_session_tag(session_id: str, body: TagSet) -> dict:
    conn = _open_idx()
    try:
        if body.tag is not None and body.tag not in _vs.TAG_VOCAB:
            raise HTTPException(400, f"unknown tag {body.tag!r}")
        if body.tag is not None:
            n = len(_vs.get_slugs_by_tag(conn, body.tag))
            _chat.set_session_kv(conn, session_id, "tag", body.tag)
        else:
            n = 0
            _chat.set_session_kv(conn, session_id, "tag", None)
        return {"session_id": session_id, "tag": body.tag, "slugs": n}
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    # ponytail: UI is one HTML file inline. Vanilla JS — no React, no build
    # step. ~5 KB so the entire UI fits in one roundtrip.
    return _INDEX_HTML


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>video-pipeline chat</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.4 ui-sans-serif, system-ui, sans-serif;
         margin: 0; height: 100vh; display: grid;
         grid-template-columns: 320px 1fr; grid-template-rows: auto 1fr auto;
         gap: 0; }
  #sidebar { grid-row: 1 / span 3; grid-column: 1; border-right: 1px solid #8883;
             overflow-y: auto; padding: 12px; background: #8881; }
  #main { grid-column: 2; display: flex; flex-direction: column;
          min-height: 0; }
  #header { padding: 12px 16px; border-bottom: 1px solid #8883;
            display: flex; gap: 12px; align-items: center;
            justify-content: space-between; }
  #header code { font-size: 12px; color: #888; }
  #log { padding: 12px 16px; overflow-y: auto; flex: 1;
         display: flex; flex-direction: column; gap: 8px; }
  .msg { padding: 8px 12px; border-radius: 8px; max-width: 80ch; }
  .msg.u { background: #2563eb14; align-self: flex-start; }
  .msg.m { background: #16a34a14; align-self: flex-start; }
  .msg .role { font-size: 11px; font-weight: 600; text-transform: uppercase;
               letter-spacing: .05em; color: #888; margin-bottom: 4px; }
  .chunks { margin-top: 6px; font-size: 12px; color: #888;
            border-top: 1px dashed #8883; padding-top: 6px; }
  .chunks summary { cursor: pointer; }
  .chunks li { list-style: none; margin: 4px 0; }
  form { padding: 12px 16px; border-top: 1px solid #8883;
         display: flex; gap: 8px; }
  input[type=text] { flex: 1; padding: 8px 12px; border: 1px solid #8883;
                     border-radius: 6px; font: inherit;
                     background: transparent; color: inherit; }
  button { padding: 8px 14px; border: 0; border-radius: 6px;
           background: #2563eb; color: white; cursor: pointer;
           font: inherit; }
  button.ghost { background: transparent; color: inherit;
                 border: 1px solid #8883; }
  select { font: inherit; padding: 6px 10px; border-radius: 6px;
           border: 1px solid #8883; background: transparent;
           color: inherit; }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  .chip { padding: 3px 8px; border-radius: 999px; background: #16a34a14;
          font-size: 12px; }
  code.k { font-family: ui-monospace, monospace; font-size: 12px; }
  /* sidebar */
  #sidebar h3 { margin: 0 0 6px 0; font-size: 12px; color: #888;
               text-transform: uppercase; letter-spacing: .04em; }
  #sidebar .filters { display: flex; gap: 6px; margin-bottom: 8px;
                      flex-wrap: wrap; }
  #sidebar .filters select { padding: 4px 8px; font-size: 12px;
                             flex: 1; min-width: 0; }
  #vlist { list-style: none; padding: 0; margin: 0; }
  #vlist li { padding: 6px 8px; border-radius: 6px; cursor: pointer;
              display: flex; gap: 8px; align-items: flex-start;
              border: 1px solid transparent; }
  #vlist li:hover { background: #8882; }
  #vlist li.selected { background: #2563eb22; border-color: #2563eb55; }
  #vlist .slug { font-family: ui-monospace, monospace; font-size: 12px;
                color: #888; }
  #vlist .meta { font-size: 11px; color: #888; }
  #vlist input[type=checkbox] { margin-top: 2px; }
  #vlist .no-tx { font-size: 11px; color: #f59e0b; }
  #vlist .ok-tx { font-size: 11px; color: #16a34a; }
  #mode-banner { padding: 4px 8px; border-radius: 4px; font-size: 11px;
                background: #2563eb22; color: #2563eb; margin-left: 8px; }
</style>
</head>
<body>
<aside id="sidebar">
  <h3>videos</h3>
  <div class="filters">
    <select id="f-mode"><option value="">all modes</option>
      <option value="transcript">transcript</option>
      <option value="multimodal">multimodal</option></select>
    <select id="f-tag"><option value="">all tags</option></select>
    <select id="f-outcome"><option value="">all outcomes</option>
      <option value="ok">ok</option>
      <option value="skip-no-transcript">skip-no-transcript</option>
      <option value="skip-junk">skip-junk</option></select>
  </div>
  <ul id="vlist"><li style="opacity:.5">loading…</li></ul>
</aside>
<div id="main">
  <div id="header">
    <div>
      <strong>video-pipeline</strong>
      <span id="mode-banner">mode: vector</span>
      <span style="color:#888;font-size:12px;">session
        <code id="sid" class="k">default</code></span>
    </div>
    <div class="chips">
      <span id="sel-count" class="chip">0 selected</span>
      <button class="ghost" id="clear-sel" type="button">clear</button>
      <button class="ghost" id="new-session" type="button">new session</button>
    </div>
  </div>
  <div id="log"></div>
  <form id="qform">
    <input type="text" id="q"
           placeholder="ask across the corpus, or check videos on the left to scope the question to them"
           autocomplete="off" required>
    <button type="submit">send</button>
  </form>
</div>
<script>
const $ = (s) => document.querySelector(s);
const log = $('#log');
let sessionId = 'default';
let activeTag = '';
let selectedSlugs = new Set();
let currentMode = 'vector';  // updated by every /api/query response

const TAG_OPTIONS = ['ai-tooling','founder-psychology','investing',
  'personal-development','religion-or-faith','history-or-politics',
  'music-or-performance','lifestyle-or-cooking','other'];

async function loadTags() {
  const r = await fetch('/api/sessions/' + encodeURIComponent(sessionId));
  if (!r.ok) return;
  const d = await r.json();
  activeTag = d.active_tag || '';
  const sel = $('#f-tag');
  sel.innerHTML = '<option value="">all tags</option>' +
    TAG_OPTIONS.map(t =>
      `<option ${t===activeTag?'selected':''}>${t}</option>`).join('');
}

async function loadHistory() {
  const r = await fetch('/api/sessions/' + encodeURIComponent(sessionId));
  if (!r.ok) return;
  const d = await r.json();
  log.innerHTML = '';
  for (const m of d.messages) render(m.role, m.content, []);
}

function escapeHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;')
       .replace(/>/g,'&gt;'); }

function render(role, content, chunks, mode) {
  const div = document.createElement('div');
  div.className = 'msg ' + (role === 'user' ? 'u' : 'm');
  const modeTag = mode ? ` <span style="color:#888;font-size:11px;">[${mode}]</span>` : '';
  div.innerHTML = `<div class="role">${role}${modeTag}</div>` +
    `<div class="body">${escapeHtml(content)}</div>`;
  if (role === 'model' && chunks && chunks.length) {
    const details = document.createElement('details');
    details.className = 'chunks';
    let list = '<summary>retrieved (' + chunks.length + ')</summary><ul>';
    for (const c of chunks) {
      list += `<li><code class="k">${c.slug}</code> ` +
              `dist=${c.distance.toFixed(3)}<br>${escapeHtml(c.text)}</li>`;
    }
    list += '</ul>';
    details.innerHTML = list;
    div.appendChild(details);
  }
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function updateModeBanner(mode) {
  currentMode = mode;
  $('#mode-banner').textContent = 'mode: ' + mode;
}

function updateSelCount() {
  const n = selectedSlugs.size;
  $('#sel-count').textContent = n + ' selected';
}

async function loadVideos() {
  const params = new URLSearchParams();
  if ($('#f-mode').value) params.set('mode', $('#f-mode').value);
  if ($('#f-tag').value) params.set('tag', $('#f-tag').value);
  if ($('#f-outcome').value) params.set('outcome', $('#f-outcome').value);
  params.set('limit', '200');
  const r = await fetch('/api/videos?' + params);
  if (!r.ok) { $('#vlist').innerHTML = '<li>load failed</li>'; return; }
  const d = await r.json();
  const ul = $('#vlist');
  ul.innerHTML = '';
  if (d.videos.length === 0) {
    ul.innerHTML = '<li style="opacity:.5">no videos match filters</li>';
    return;
  }
  for (const v of d.videos) {
    const li = document.createElement('li');
    const checked = selectedSlugs.has(v.slug);
    if (checked) li.classList.add('selected');
    li.innerHTML =
      `<input type="checkbox" ${checked?'checked':''}>` +
      `<div style="flex:1;min-width:0">` +
        `<div class="slug">${v.slug}</div>` +
        `<div class="meta">${v.mode} · ${v.outcome} · ${v.analyzed_on}` +
        (v.tags.length ? ' · ' + v.tags.join(', ') : '') + `</div>` +
        (v.has_transcript
          ? '<div class="ok-tx">transcript ✓</div>'
          : '<div class="no-tx">no transcript (multimodal-only)</div>') +
      `</div>`;
    const cb = li.querySelector('input');
    cb.addEventListener('click', (e) => {
      e.stopPropagation();
      if (cb.checked) selectedSlugs.add(v.slug);
      else selectedSlugs.delete(v.slug);
      li.classList.toggle('selected', cb.checked);
      updateSelCount();
    });
    li.addEventListener('click', (e) => {
      if (e.target.tagName === 'INPUT') return;
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event('click'));
    });
    ul.appendChild(li);
  }
}

$('#qform').addEventListener('submit', async (e) => {
  e.preventDefault();
  const question = $('#q').value.trim();
  if (!question) return;
  $('#q').value = '';
  const explicitSlugs = Array.from(selectedSlugs);
  const mode = explicitSlugs.length > 0 ? 'url-list' : 'vector';
  const banner = `${explicitSlugs.length} slug(s)`;
  render('user', `${question}\n\n— ${banner} —`, [], mode);
  const body = { question, session_id: sessionId };
  if (activeTag) body.tag = activeTag;
  if (explicitSlugs.length) body.slugs = explicitSlugs;
  const r = await fetch('/api/query', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body),
  });
  if (!r.ok) { render('model', 'ERROR: ' + (await r.text()), [], mode); return; }
  const d = await r.json();
  updateModeBanner(d.mode || mode);
  render('model', d.reply, d.chunks, d.mode || mode);
});

['f-mode','f-tag','f-outcome'].forEach(id =>
  $('#'+id).addEventListener('change', loadVideos));

$('#clear-sel').addEventListener('click', () => {
  selectedSlugs.clear();
  document.querySelectorAll('#vlist li.selected').forEach(li => {
    li.classList.remove('selected');
    const cb = li.querySelector('input'); if (cb) cb.checked = false;
  });
  updateSelCount();
});

$('#new-session').addEventListener('click', async () => {
  const sid = 'web-' + Date.now();
  const r = await fetch('/api/sessions/' + encodeURIComponent(sid),
    { method: 'GET' });
  if (r.ok) {
    sessionId = sid;
    $('#sid').textContent = sessionId;
    log.innerHTML = '';
    loadTags();
  }
});

loadTags();
loadHistory();
loadVideos();
updateSelCount();
</script>
</body>
</html>
"""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"HTTP port (default: {DEFAULT_PORT})")
    p.add_argument("--reload", action="store_true",
                   help="reload on code change (dev only)")
    args = p.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("uvicorn not installed; pip install -r requirements.txt",
              file=sys.stderr)
        return 2

    uvicorn.run("web:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
