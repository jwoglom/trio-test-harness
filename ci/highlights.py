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
    return json.loads(raw[start:end + 1])


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
