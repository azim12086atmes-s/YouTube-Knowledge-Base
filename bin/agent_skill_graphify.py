"""agent_skill_graphify.py — repo-graph wrapper for the next agent.

ponytail: graphify is the AI-coding skill the user named that
directly addresses the context-bloat pain. The graph it produces
(via `graphify extract .`) is a *folder-indexed* knowledge graph:
each node is a file or function, edges are call/contain/import.
The next agent can answer "where is X defined?" in 500 tokens
instead of loading 6700 LOC of Python into context.

This wrapper is the project's typed interface to graphify. It:

  - Runs `graphify extract --code-only <path>` to (re)build the
    graph. Idempotent; safe to re-run after a code change.
  - Surfaces `query`, `path`, `explain`, `affected` subcommands
    for the next agent. Each writes a structured result to
    `graphify-out/agent-skill-results.json` so the next agent
    can read without re-invoking graphify.
  - Refuses to call Gemini / any LLM by default. The --code-only
    path is local AST. If docs need semantic extraction, the
    caller must pass --with-llm explicitly (and have an API key).

The graph is rebuilt incrementally. On every `bin/agent_loop.py`
tick, this script's `rebuild_if_stale` checks mtime of any .py
file under bin/ against graph.json's mtime; if stale, rebuild.

Why this exists separately from `graphify install --platform hermes`:

  - The Hermes skill teaches the *agent* how to call graphify.
  - This script teaches the *project* how to use graphify —
    project-specific paths, project-specific re-build policy,
    project-specific output schema.

The two are complementary, not redundant.
"""

from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GRAPH_OUT = PROJECT_ROOT / "graphify-out"
GRAPH_JSON = GRAPH_OUT / "graph.json"
RESULTS_JSON = GRAPH_OUT / "agent-skill-results.json"
PY = sys.executable


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    """Run a subprocess, return (rc, stdout, stderr) joined."""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def rebuild(with_llm: bool = False, target: str = ".") -> int:
    """Re-run `graphify extract <target>` and write a fresh graph.

    with_llm=False: AST only, no API key needed.
    with_llm=True:  semantic extraction of docs (needs an LLM key
                    in the env). Slower; not used in cron.
    """
    GRAPH_OUT.mkdir(exist_ok=True)
    cmd = [PY, "-m", "graphify", "extract", str(target)]
    if not with_llm:
        cmd.append("--code-only")
    print(f"# agent_skill_graphify: rebuild — {' +LLM' if with_llm else 'code-only'}",
          file=sys.stderr)
    rc, out, err = _run(cmd, timeout=600)
    print(out, file=sys.stdout)
    print(err, file=sys.stderr)
    if rc != 0:
        print(f"# rebuild failed (rc={rc})", file=sys.stderr)
    return rc


def is_stale(target_dir: Path = PROJECT_ROOT) -> bool:
    """True if any .py file under target_dir is newer than graph.json.

    The graph.json mtime is the build timestamp; if any source file
    is newer, the graph is out of date. The build itself is cheap
    (~3s on this repo at 6.7k LOC), so being conservative is fine.
    """
    if not GRAPH_JSON.exists():
        return True
    g_mtime = GRAPH_JSON.stat().st_mtime
    for p in target_dir.rglob("*.py"):
        if p.stat().st_mtime > g_mtime:
            return True
    return False


def query(q: str, budget: int = 2000) -> dict:
    """BFS traversal of the graph for a question.

    Returns the parsed graphify output as a dict. The next agent
    can read this without re-invoking graphify.
    """
    if not GRAPH_JSON.exists():
        return {"error": "graph.json missing — run `agent_skill_graphify.py rebuild` first"}
    rc, out, err = _run([PY, "-m", "graphify", "query", q, "--budget", str(budget)])
    return {"rc": rc, "question": q, "output": out, "stderr": err}


def path(a: str, b: str, budget: int = 1000) -> dict:
    """Shortest path between two nodes in the graph."""
    if not GRAPH_JSON.exists():
        return {"error": "graph.json missing — run rebuild first"}
    rc, out, err = _run([PY, "-m", "graphify", "path", a, b, "--budget", str(budget)])
    return {"rc": rc, "from": a, "to": b, "output": out, "stderr": err}


def explain(node: str) -> dict:
    """Plain-language explanation of a node + its neighbors."""
    if not GRAPH_JSON.exists():
        return {"error": "graph.json missing — run rebuild first"}
    rc, out, err = _run([PY, "-m", "graphify", "explain", node])
    return {"rc": rc, "node": node, "output": out, "stderr": err}


def affected(node: str, depth: int = 2) -> dict:
    """Reverse traversal: what does this node impact?"""
    if not GRAPH_JSON.exists():
        return {"error": "graph.json missing — run rebuild first"}
    rc, out, err = _run([PY, "-m", "graphify", "affected", node, "--depth", str(depth)])
    return {"rc": rc, "node": node, "depth": depth, "output": out, "stderr": err}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    rb = sub.add_parser("rebuild", help="rebuild the graph")
    rb.add_argument("--with-llm", action="store_true",
                    help="enable semantic extraction of docs (needs LLM key)")
    rb.add_argument("--target", default=".",
                    help="path to extract (default: project root)")

    sub.add_parser("status", help="print graph.json mtime + size + node count")

    qu = sub.add_parser("query", help="BFS for a question")
    qu.add_argument("question")
    qu.add_argument("--budget", type=int, default=2000)

    pa = sub.add_parser("path", help="shortest path between two nodes")
    pa.add_argument("a")
    pa.add_argument("b")
    pa.add_argument("--budget", type=int, default=1000)

    ex = sub.add_parser("explain", help="plain-language explanation of a node")
    ex.add_argument("node")

    af = sub.add_parser("affected", help="reverse traversal: what does X impact?")
    af.add_argument("node")
    af.add_argument("--depth", type=int, default=2)

    rs = sub.add_parser("rebuild_if_stale",
                        help="rebuild only if any .py file is newer than graph.json")
    rs.add_argument("--with-llm", action="store_true")

    args = p.parse_args()

    if args.cmd == "rebuild":
        return rebuild(with_llm=args.with_llm, target=args.target)
    if args.cmd == "status":
        if not GRAPH_JSON.exists():
            print("# graph.json missing — run rebuild")
            return 1
        st = GRAPH_JSON.stat()
        # peek at the node count from the JSON
        try:
            data = json.loads(GRAPH_JSON.read_text(encoding="utf-8", errors="replace"))
            n_nodes = len(data.get("nodes", []))
            n_edges = len(data.get("links", data.get("edges", [])))
        except Exception:
            n_nodes = n_edges = "?"
        print(f"graph.json: {GRAPH_JSON}")
        print(f"  size: {st.st_size} bytes")
        print(f"  mtime: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))}")
        print(f"  nodes: {n_nodes}  edges: {n_edges}")
        return 0
    if args.cmd == "query":
        result = query(args.question, budget=args.budget)
    elif args.cmd == "path":
        result = path(args.a, args.b, budget=args.budget)
    elif args.cmd == "explain":
        result = explain(args.node)
    elif args.cmd == "affected":
        result = affected(args.node, depth=args.depth)
    elif args.cmd == "rebuild_if_stale":
        if is_stale():
            print("# agent_skill_graphify: graph stale — rebuilding",
                  file=sys.stderr)
            return rebuild(with_llm=args.with_llm)
        print("# agent_skill_graphify: graph fresh — no rebuild",
              file=sys.stderr)
        return 0

    # Save the result for the next agent to read without re-invoking.
    GRAPH_OUT.mkdir(exist_ok=True)
    RESULTS_JSON.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return result.get("rc", 0)


if __name__ == "__main__":
    raise SystemExit(main())
