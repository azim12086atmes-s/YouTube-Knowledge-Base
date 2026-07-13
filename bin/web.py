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
  body { font: 14px/1.4 ui-sans-serif, system-ui, sans-serif; max-width: 800px;
         margin: 24px auto; padding: 0 16px; }
  #header { display: flex; gap: 12px; align-items: center;
            justify-content: space-between; margin-bottom: 12px; }
  #header code { font-size: 12px; color: #888; }
  #log { border: 1px solid #ccc3; border-radius: 8px; padding: 12px;
         height: 60vh; overflow-y: auto; background: #f8f8fa08; }
  .msg { margin: 8px 0; padding: 6px 8px; border-radius: 6px; }
  .msg.u { background: #2563eb14; }
  .msg.m { background: #16a34a14; }
  .msg .role { font-size: 11px; font-weight: 600; text-transform: uppercase;
               letter-spacing: .05em; color: #888; margin-bottom: 4px; }
  .chunks { margin-top: 6px; font-size: 12px; color: #888;
            border-top: 1px dashed #8883; padding-top: 6px; }
  .chunks summary { cursor: pointer; }
  .chunks li { list-style: none; margin: 4px 0; }
  form { display: flex; gap: 8px; margin-top: 12px; }
  input[type=text] { flex: 1; padding: 8px 12px; border: 1px solid #ccc3;
                     border-radius: 6px; font: inherit; }
  button { padding: 8px 14px; border: 0; border-radius: 6px; background: #2563eb;
           color: white; cursor: pointer; font: inherit; }
  button.ghost { background: transparent; color: inherit; border: 1px solid #8883; }
  select { font: inherit; padding: 6px 10px; border-radius: 6px;
           border: 1px solid #ccc3; background: transparent; color: inherit; }
  details { margin-top: 6px; }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  .chip { padding: 3px 8px; border-radius: 999px; background: #16a34a14;
          font-size: 12px; }
  code.k { font-family: ui-monospace, monospace; font-size: 12px; }
</style>
</head>
<body>
<div id="header">
  <div>
    <strong>video-pipeline</strong> <span id="session-label"
      style="color:#888;font-size:12px;">session <code id="sid"
      class="k">default</code></span>
  </div>
  <div class="chips">
    <select id="tag"><option value="">no tag filter</option></select>
    <button class="ghost" id="new-session" type="button">new session</button>
  </div>
</div>
<div id="log"></div>
<form id="qform">
  <input type="text" id="q" placeholder="ask about your watch history…"
         autocomplete="off" required>
  <button type="submit">send</button>
</form>
<script>
const $ = (s) => document.querySelector(s);
const log = $('#log');
let sessionId = 'default';
let activeTag = '';

async function loadTags() {
  const r = await fetch('/api/sessions/' + encodeURIComponent(sessionId));
  if (!r.ok) return;
  const d = await r.json();
  activeTag = d.active_tag || '';
  const sel = $('#tag');
  sel.innerHTML = '<option value=\"\">no tag filter</option>' +
    ['ai-tooling','founder-psychology','investing','personal-development',
     'religion-or-faith','history-or-politics','music-or-performance',
     'lifestyle-or-cooking','other']
     .map(t => `<option ${t===activeTag?'selected':''}>${t}</option>`).join('');
}

async function loadHistory() {
  const r = await fetch('/api/sessions/' + encodeURIComponent(sessionId));
  if (!r.ok) return;
  const d = await r.json();
  log.innerHTML = '';
  for (const m of d.messages) render(m.role, m.content, []);
}

function render(role, content, chunks) {
  const div = document.createElement('div');
  div.className = 'msg ' + (role === 'user' ? 'u' : 'm');
  div.innerHTML = `<div class="role">${role}</div>` +
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

function escapeHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;')
       .replace(/>/g,'&gt;'); }

$('#qform').addEventListener('submit', async (e) => {
  e.preventDefault();
  const question = $('#q').value.trim();
  if (!question) return;
  $('#q').value = '';
  render('user', question, []);
  const body = { question, session_id: sessionId };
  if (activeTag) body.tag = activeTag;
  const r = await fetch('/api/query', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body),
  });
  if (!r.ok) { render('model', 'ERROR: ' + (await r.text()), []); return; }
  const d = await r.json();
  render('model', d.reply, d.chunks);
});

$('#tag').addEventListener('change', async () => {
  const tag = $('#tag').value;
  const r = await fetch('/api/sessions/' + encodeURIComponent(sessionId) + '/tag',
    { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ tag: tag || null }) });
  if (r.ok) { activeTag = tag; }
});

$('#new-session').addEventListener('click', async () => {
  const sid = 'web-' + Date.now();
  const r = await fetch('/api/sessions/' + encodeURIComponent(sid),
    { method: 'GET' });  // creates session on first save_message
  if (r.ok) {
    sessionId = sid;
    $('#sid').textContent = sessionId;
    log.innerHTML = '';
    loadTags();
  }
});

loadTags();
loadHistory();
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
