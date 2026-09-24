"""Write the GitHub Actions job summary for a trio-test-e2e run (markdown on stdout).

    python ci/summary.py <run dir> [--agent-summary FILE] [--comment-note TEXT]

Screenshots cannot be shown inline in a job summary (they need a public URL), so the
summary lists the steps and points at the run's artifact for report.html.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ICON = {"passed": "✅", "failed": "❌", "partial": "⚠️", "running": "⏳"}


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run_dir", nargs="?")
    p.add_argument("--agent-summary")
    p.add_argument("--comment-note")
    args = p.parse_args(argv)

    out = []
    run = Path(args.run_dir) if args.run_dir else None
    if not run or not (run / "run.json").exists():
        out.append("## Trio e2e: no run was created\n\nThe job failed before the harness started a run; see the step logs.")
        print("\n".join(out))
        return 0

    meta = json.loads((run / "run.json").read_text())
    status = meta.get("status", "running")
    out.append(f"## {ICON.get(status, '')} {meta.get('title') or meta.get('name')}")
    out.append("")
    out.append(f"- **Status:** {status}" + (f": {meta['summary']}" if meta.get("summary") else ""))
    if meta.get("pr_url"):
        out.append(f"- **PR:** {meta['pr_url']}")
    build = meta.get("build") or {}
    git = build.get("git") or {}
    built = {"Trio": git.get("trio_branch") or git.get("branch"), "commit": git.get("sha"),
             "configuration": build.get("configuration"), "Xcode": build.get("xcode")}
    built = {k: v for k, v in built.items() if v}
    if built:
        out.append("- **Built:** " + ", ".join(f"{k} `{v}`" for k, v in built.items()))
    subs = git.get("submodules") or {}
    if isinstance(subs, dict) and subs:
        out.append("- **Submodules:** " + ", ".join(f"{k} `{str(v)[:12]}`" for k, v in sorted(subs.items())))

    steps = []
    if (run / "steps.jsonl").exists():
        for line in (run / "steps.jsonl").read_text().splitlines():
            try:
                steps.append(json.loads(line))
            except ValueError:
                pass
    if steps:
        out += ["", "| # | Time | Step | Note |", "|---|---|---|---|"]
        for i, s in enumerate(steps, 1):
            note = str(s.get("note", "")).replace("|", "\\|").replace("\n", " ")
            shot = " 📷" if s.get("screenshot") else ""
            out.append(f"| {i} | {str(s.get('t', ''))[11:19]} | {s.get('action', '')}{shot} | {note[:200]} |")

    if args.agent_summary and Path(args.agent_summary).exists():
        text = Path(args.agent_summary).read_text().strip()
        if text:
            out += ["", "### Agent findings", "", text]
    if args.comment_note:
        out += ["", f"**PR comment:** {args.comment_note}"]
    out += ["", "Screenshots and `report.html` are in this run's artifact (Summary ▸ Artifacts)."]
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
