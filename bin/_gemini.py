"""_gemini.py — shared Gemini HTTP helper, used by analyze / ask / chat.

One POST function, three callers. Stdlib only (urllib + json).

ponytail: model is parameterized so callers pick between the cheap
text bucket (gemini-flash-lite-latest — analyze.py's four-shape prompts)
and the bigger multimodal-bucket text path (gemini-3.1-flash-lite —
used by ask.py and chat.py for conversational answers).
"""
from __future__ import annotations
import json
import urllib.error
import urllib.request
from pathlib import Path


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
ENV_PATH = Path.home() / "AppData" / "Local" / "hermes" / ".env"


def gemini_key(env_path: Path | None = None) -> str:
    """Read GEMINI_API_KEY from ~/.hermes/.env. Raises if missing."""
    p = env_path or ENV_PATH
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("GEMINI_API_KEY="):
            return line.split("=", 1)[1]
    raise SystemExit(f"GEMINI_API_KEY missing from {p} — pause and ask.")


def post(contents: list[dict], api_key: str, model: str,
         *, temperature: float = 0.3, max_output_tokens: int = 4096,
         timeout: int = 120) -> str:
    """POST `contents` to Gemini. Returns text or 'ERROR <code>: ...'.

    ponytail: error envelopes return a parseable string so callers can
    branch on `response.startswith("ERROR")`.
    """
    body = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
        },
    }
    url = f"{GEMINI_URL}/{model}:generateContent?key={api_key}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
            parts = data["candidates"][0]["content"].get("parts") or []
            return "".join(p.get("text", "") for p in parts) or "(empty response)"
    except urllib.error.HTTPError as e:
        return f"ERROR {e.code}: {e.read().decode()[:300]}"
    except Exception as e:
        return f"ERROR {type(e).__name__}: {e}"


def post_text(prompt: str, api_key: str, model: str,
              *, temperature: float = 0.3, max_output_tokens: int = 4096,
              timeout: int = 120) -> str:
    """Convenience wrapper: one user-message text prompt."""
    return post(
        [{"role": "user", "parts": [{"text": prompt}]}],
        api_key, model,
        temperature=temperature, max_output_tokens=max_output_tokens,
        timeout=timeout,
    )


# ponytail: self-check is a real invocation against the live API. Skipped
# unless GEMINI_API_KEY is set + a `--self-check` flag is supplied, since
# free-tier quota is precious. Module is otherwise side-effect-free on import.
if __name__ == "__main__":
    import sys
    if "--self-check" in sys.argv:
        key = gemini_key()
        out = post_text("Reply with the literal text 'OK' and nothing else.",
                        key, "gemini-flash-lite-latest", max_output_tokens=8)
        if "OK" in out:
            print("OK  _gemini self-check passed")
        else:
            print(f"FAIL _gemini self-check: {out[:200]!r}")
            sys.exit(1)
    else:
        print("# _gemini.py — shared POST helper for analyze/ask/chat")
        print("# pass --self-check to probe the live API (burns 1 call)")
