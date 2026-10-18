"""Probe a model gateway: which models answer, and which return well-formed tool calls?

    python -m vpsagents.probe --base-url http://127.0.0.1:3001/v1 --key-env FREELLMAPI_API_KEY [--models a,b] [--limit 10]

Free models differ wildly in tool-calling reliability; run this before choosing worker/reviewer models.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

TOOL = {"type": "function", "function": {
    "name": "write_file", "description": "Write text to a file.",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                   "required": ["path", "content"]}}}


def _post(url: str, key: str, body: dict, timeout: int = 90) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - operator-supplied URL
        return json.loads(r.read())


def list_models(base: str, key: str) -> list[str]:
    req = urllib.request.Request(f"{base.rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        return [m["id"] for m in json.loads(r.read()).get("data", [])]


def probe_model(base: str, key: str, model: str) -> dict:
    t0 = time.time()
    res = {"model": model, "chat": False, "tool_call": False, "args_valid": False, "seconds": None, "error": None}
    try:
        _post(f"{base.rstrip('/')}/chat/completions", key,
              {"model": model, "messages": [{"role": "user", "content": "Reply with the single word: pong"}], "max_tokens": 16})
        res["chat"] = True
        out = _post(f"{base.rstrip('/')}/chat/completions", key, {
            "model": model, "tools": [TOOL], "tool_choice": "auto", "max_tokens": 200,
            "messages": [{"role": "user", "content": "Call write_file with path 'hello.txt' and content 'hi'."}]})
        calls = (out["choices"][0]["message"].get("tool_calls") or [])
        res["tool_call"] = bool(calls)
        if calls:
            args = json.loads(calls[0]["function"]["arguments"])
            res["args_valid"] = args.get("path") == "hello.txt" and "content" in args
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    res["seconds"] = round(time.time() - t0, 1)
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--key-env", default="FREELLMAPI_API_KEY")
    ap.add_argument("--models", default="")
    ap.add_argument("--limit", type=int, default=10)
    a = ap.parse_args(argv)
    key = os.environ.get(a.key_env, "")
    models = [m for m in a.models.split(",") if m] or list_models(a.base_url, key)[: a.limit]
    results = [probe_model(a.base_url, key, m) for m in models]
    print(f"{'model':40} chat  tool  args  secs  error")
    for r in results:
        print(f"{r['model'][:40]:40} {'ok' if r['chat'] else '-':5} {'ok' if r['tool_call'] else '-':5} "
              f"{'ok' if r['args_valid'] else '-':5} {r['seconds']:<5} {r['error'] or ''}")
    return 0 if any(r["args_valid"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
