"""Readable live progress from `claude -p --output-format stream-json --verbose` (stdin).

    claude -p ... --output-format stream-json --verbose | python3 ci/agent-stream.py SUMMARY_FILE

Prints the model, each tool call, the agent's own short notes and errors as they happen; writes the
final answer to SUMMARY_FILE (it becomes the job summary's "Agent findings"). Exits non-zero if
Claude reported an error.
"""
from __future__ import annotations

import json
import sys


def short(text: str, n: int = 200) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def describe(name: str, inp: dict) -> str:
    if name == "Bash":
        return short(inp.get("command", ""))
    for key in ("file_path", "path", "pattern"):
        if inp.get(key):
            return short(inp[key])
    return short(json.dumps(inp))


def main() -> int:
    out_path = sys.argv[1] if len(sys.argv) > 1 else None
    result, is_error, last_text = "", False, ""
    for line in sys.stdin:
        try:
            ev = json.loads(line)
        except ValueError:
            print(line.rstrip(), flush=True)
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            print(f"agent model: {ev.get('model')} (Claude Code {ev.get('claude_code_version', '?')})", flush=True)
        elif kind == "assistant":
            for c in ev.get("message", {}).get("content", []):
                if c.get("type") == "tool_use":
                    print(f"▶ {c.get('name')}: {describe(c.get('name', ''), c.get('input') or {})}", flush=True)
                elif c.get("type") == "text" and c.get("text", "").strip():
                    last_text = c["text"].strip()
                    print(f"💬 {short(c['text'], 300)}", flush=True)
        elif kind == "user":
            for c in ev.get("message", {}).get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                    body = c.get("content")
                    if isinstance(body, list):
                        body = " ".join(str(b.get("text", "")) for b in body if isinstance(b, dict))
                    print(f"  ✘ {short(body, 240)}", flush=True)
        elif kind == "result":
            result = ev.get("result") or ""
            is_error = bool(ev.get("is_error"))
            cost = ev.get("total_cost_usd")
            secs = (ev.get("duration_ms") or 0) / 1000
            print(f"agent finished: {ev.get('subtype')}, {ev.get('num_turns')} turns, {secs:.0f}s"
                  + (f", ${cost:.2f}" if isinstance(cost, (int, float)) else ""), flush=True)
    if out_path:
        with open(out_path, "w") as f:
            f.write(result.strip() + "\n" if result else "")
    if result and short(result, 300) != short(last_text, 300):
        print("\n" + result, flush=True)
    return 1 if is_error else 0


if __name__ == "__main__":
    sys.exit(main())
