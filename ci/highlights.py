"""Validate the highlights reviewer's answer and store it as <run>/highlights.json.

    python ci/highlights.py RUN_DIR RAW_ANSWER_FILE

The answer should be a JSON object (verdict, summary, highlights[{screenshot, caption}]). Screenshots that do not
exist in the run are dropped and at most 10 are kept (the reviewer is asked for only as many as the change needs).
On any problem nothing is written and the report keeps its plain layout; the exit code is 0 either way.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

VERDICTS = {"works", "broken", "inconclusive"}
MAX_HIGHLIGHTS = 10


def parse(raw: str) -> dict:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the answer")
    text = raw[start:end + 1]
    try:
        return json.loads(text, strict=False)    # strict=False: a raw newline inside a string is kept, not refused
    except ValueError:
        # A reviewer quoting a label in its prose ("Pump Event Not Counted") leaves the quotes unescaped.
        return json.loads(escape_stray_quotes(text), strict=False)


def escape_stray_quotes(text: str) -> str:
    """Escapes a double quote inside a JSON string that is not followed by what can come after a string's end
    (`,` `:` `}` `]` or the end), which is how an unescaped quote in prose looks."""
    out, in_string, i = [], False, 0
    while i < len(text):
        c = text[i]
        if in_string and c == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == '"':
            if not in_string:
                in_string = True
            else:
                rest = text[i + 1:].lstrip()
                if rest and rest[0] not in ",:}]":
                    out.append('\\"')
                    i += 1
                    continue
                in_string = False
        out.append(c)
        i += 1
    return "".join(out)


def validate(doc: dict, run: Path) -> dict:
    verdict = str(doc.get("verdict", "")).strip().lower()
    summary = str(doc.get("summary", "")).strip()
    if verdict not in VERDICTS or not summary:
        raise ValueError(f"missing verdict/summary (verdict={verdict!r})")
    shots = []
    for h in doc.get("highlights") or []:
        rel = str(h.get("screenshot", "")).strip().lstrip("./")
        if rel and (run / rel).is_file() and rel not in [s["screenshot"] for s in shots]:
            shots.append({"screenshot": rel, "caption": str(h.get("caption", "")).strip()})
    if not shots:
        raise ValueError("none of the chosen screenshots exist in the run")
    return {"verdict": verdict, "summary": summary, "highlights": shots[:MAX_HIGHLIGHTS]}


def main() -> int:
    run, raw = Path(sys.argv[1]), Path(sys.argv[2])
    try:
        doc = validate(parse(raw.read_text()), run)
    except (OSError, ValueError) as e:
        print(f"::warning title=Highlights skipped::{e}")
        return 0
    (run / "highlights.json").write_text(json.dumps(doc, indent=1) + "\n")
    print(f"highlights: {doc['verdict']}, {len(doc['highlights'])} screenshot(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
