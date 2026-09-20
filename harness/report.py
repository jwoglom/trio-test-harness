"""Test-run bookkeeping and reporting.

A *run* is a directory under runs/ holding numbered screenshots, a step log
(steps.jsonl) and run.json metadata. From a run we can render:
  * report.html  – self-contained page with embedded screenshots (publish as a claude.ai Artifact)
  * a GitHub PR comment / description section with the screenshots attached inline (gh --attach)
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import io
import json
import re
import shutil
import subprocess
from pathlib import Path

from .config import CONFIG, REPO_ROOT
from .util import HarnessError, log, now_slug, read_json, run, write_json

CURRENT = REPO_ROOT / "runs" / ".current"


class Run:
    def __init__(self, path: Path):
        self.path = path
        self.shots = path / "screenshots"
        self.shots.mkdir(parents=True, exist_ok=True)
        self.meta_path = path / "run.json"
        self.steps_path = path / "steps.jsonl"

    @classmethod
    def new(cls, name: str, meta: dict | None = None) -> "Run":
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-") or "run"
        r = cls(CONFIG.runs_dir / f"{now_slug()}-{slug}")
        m = {"name": name, "created": dt.datetime.now().isoformat(timespec="seconds"), "status": "running"}
        m.update(meta or {})
        write_json(r.meta_path, m)
        CURRENT.parent.mkdir(parents=True, exist_ok=True)
        CURRENT.write_text(str(r.path))
        return r

    @classmethod
    def current(cls, path: str | None = None) -> "Run":
        if path:
            return cls(Path(path).expanduser())
        if CURRENT.exists() and Path(CURRENT.read_text().strip()).exists():
            return cls(Path(CURRENT.read_text().strip()))
        raise HarnessError("no current run; create one with `th run new <name>` or pass --run DIR")

    @property
    def meta(self) -> dict:
        return read_json(self.meta_path, {})

    def update_meta(self, **kw) -> None:
        m = self.meta
        m.update(kw)
        write_json(self.meta_path, m)

    def steps(self) -> list[dict]:
        if not self.steps_path.exists():
            return []
        return [json.loads(l) for l in self.steps_path.read_text().splitlines() if l.strip()]

    def next_index(self) -> int:
        return len(list(self.shots.glob("*.png"))) + 1

    def shot_path(self, label: str) -> Path:
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", label).strip("-")[:60] or "shot"
        return self.shots / f"{self.next_index():03d}-{slug}.png"

    def log_step(self, action: str, note: str = "", screenshot: Path | None = None, **extra) -> dict:
        rec = {"t": dt.datetime.now().isoformat(timespec="seconds"), "action": action, "note": note}
        if screenshot:
            rec["screenshot"] = str(Path(screenshot).relative_to(self.path)) if Path(screenshot).is_absolute() else str(screenshot)
        rec.update(extra)
        with open(self.steps_path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        return rec

    def finish(self, status: str, summary: str = "") -> None:
        self.update_meta(status=status, summary=summary, finished=dt.datetime.now().isoformat(timespec="seconds"))


# ---------------- HTML report -----------------

def _thumb_b64(png: Path, max_w: int = 640) -> str:
    from PIL import Image
    im = Image.open(png)
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="JPEG", quality=80, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def render_html(run: Run, out: Path | None = None) -> Path:
    meta = run.meta
    steps = run.steps()
    title = meta.get("title") or f"Trio test: {meta.get('name','run')}"
    build = meta.get("build", {})
    git = build.get("git", {}) if isinstance(build, dict) else {}
    status = meta.get("status", "running")
    color = {"passed": "#1a7f37", "failed": "#cf222e"}.get(status, "#9a6700")
    cards = []
    for s in steps:
        img = ""
        if s.get("screenshot") and (run.path / s["screenshot"]).exists():
            img = f'<img src="{_thumb_b64(run.path / s["screenshot"])}" alt="{html.escape(s.get("note",""))}" loading="lazy">'
        cards.append(
            f'<figure><div class="frame">{img}</div><figcaption><b>{html.escape(s.get("action",""))}</b>'
            f'<span>{html.escape(s.get("note",""))}</span><time>{html.escape(s.get("t","")[11:19])}</time></figcaption></figure>')
    facts = [
        ("Target", meta.get("target", build.get("target", "?"))),
        ("Branch / PR", git.get("pr") or git.get("branch") or "in-place checkout"),
        ("Commit", f"{git.get('sha','?')} {git.get('describe','')}"),
        ("Xcode", build.get("xcode", "?")),
        ("Bundle", build.get("bundle_id", "?")),
        ("Started", meta.get("created", "?")),
    ]
    if git.get("submodule_overrides"):
        facts.append(("Submodules", "; ".join(f"{o['name']} @ {o['ref']}" for o in git["submodule_overrides"])))
    fact_html = "".join(f"<dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd>" for k, v in facts)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{--bg:#fff;--fg:#1f2328;--muted:#656d76;--card:#f6f8fa;--line:#d0d7de;--accent:{color}}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--card:#161b22;--line:#30363d}}}}
:root[data-theme="dark"]{{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--card:#161b22;--line:#30363d}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,sans-serif}}
main{{max-width:1100px;margin:0 auto}}
h1{{font-size:22px;margin:0 0 4px}} .status{{display:inline-block;padding:2px 10px;border-radius:999px;background:var(--accent);color:#fff;font-size:13px;font-weight:600;text-transform:uppercase}}
dl{{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:16px 0;padding:12px 16px;background:var(--card);border:1px solid var(--line);border-radius:8px}}
dt{{color:var(--muted)}} dd{{margin:0;word-break:break-word}}
.summary{{margin:0 0 16px;white-space:pre-wrap}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:16px}}
figure{{margin:0;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}}
.frame{{aspect-ratio:9/19.5;background:#000;display:flex;align-items:center;justify-content:center}}
.frame img{{width:100%;height:100%;object-fit:contain}}
figcaption{{padding:8px 10px;font-size:13px;display:flex;flex-direction:column;gap:2px}} figcaption span{{color:var(--muted)}} time{{color:var(--muted);font-size:11px}}
</style></head><body><main>
<h1>{html.escape(title)}</h1><span class="status">{html.escape(status)}</span>
<dl>{fact_html}</dl>
<p class="summary">{html.escape(meta.get('summary',''))}</p>
<section class="grid">{''.join(cards)}</section>
<p style="color:var(--muted);font-size:12px;margin-top:24px">Generated by trio-test-harness. Screenshots use Trio's simulator CGM/pump; no real patient data.</p>
</main></body></html>"""
    out = out or (run.path / "report.html")
    out.write_text(page)
    log(f"wrote {out} ({out.stat().st_size // 1024} KB)")
    return out


# ---------------- GitHub PR reporting -----------------
# Screenshots are uploaded *into* the comment with `gh pr comment --attach` (gh ≥ 2.99): each `![alt](./path.png)`
# reference in the body is rewritten to the uploaded asset URL. No side repo, no external hosting.

MAX_ATTACH = 50   # GitHub's per-command limit


def _gh(args: list[str], **kw) -> str:
    return run(["gh", *args], **kw).stdout


def select_steps(run: Run, max_shots: int | None) -> list[dict]:
    """Steps with screenshots, evenly thinned to max_shots (first and last always kept)."""
    steps = [s for s in run.steps() if s.get("screenshot") and (run.path / s["screenshot"]).exists()]
    limit = min(max_shots or MAX_ATTACH, MAX_ATTACH)
    if len(steps) <= limit:
        return steps
    idx = sorted({round(i * (len(steps) - 1) / (limit - 1)) for i in range(limit)})
    return [steps[i] for i in idx]


def render_markdown(run: Run, *, columns: int = 3, max_shots: int | None = None,
                    artifact_url: str | None = None) -> tuple[str, list[Path]]:
    """Comment body (relative image paths) + the files to attach, in order."""
    meta = run.meta
    build = meta.get("build", {})
    git = build.get("git", {})
    status = meta.get("status", "running")
    icon = {"passed": "✅", "failed": "❌"}.get(status, "🟡")
    lines = [f"### {icon} Trio test run: {meta.get('name','')}", ""]
    lines.append(f"- **Target:** {meta.get('mode') or meta.get('target', build.get('target','?'))}  ")
    lines.append(f"- **Commit:** `{git.get('sha','?')}` {git.get('describe','')}  ")
    if git.get("trio_branch"):
        lines.append(f"- **Trio branch:** `{git['trio_branch']}`  ")
    if git.get("submodule_overrides"):
        lines.append("- **Submodules:** " + "; ".join(f"`{o['name']}` @ `{o['ref']}`" for o in git["submodule_overrides"]) + "  ")
    lines.append(f"- **Xcode:** {build.get('xcode','?')} · **Result:** {status}")
    if artifact_url:
        lines.append(f"- **Full report:** {artifact_url}")
    if meta.get("summary"):
        lines += ["", meta["summary"]]
    steps = select_steps(run, max_shots)
    files: list[Path] = []
    if steps:
        total = len([s for s in run.steps() if s.get("screenshot")])
        note = f" ({len(steps)} of {total} screenshots)" if len(steps) < total else ""
        lines += ["", f"<details open><summary>Screenshots{note}</summary>", ""]
        header = "| " + " | ".join([" "] * columns) + " |"
        lines += [header, "|" + "---|" * columns]
        for i in range(0, len(steps), columns):
            row = steps[i:i + columns]
            cells = []
            for s in row:
                rel = s["screenshot"]
                files.append(run.path / rel)
                cap = html.escape(f"{s.get('action','')}: {s.get('note','')}".strip(": "))
                cells.append(f"![{cap}](./{rel})<br><sub>{cap}</sub>")
            cells += [" "] * (columns - len(row))
            lines.append("| " + " | ".join(cells) + " |")
        lines += ["", "</details>"]
    lines += ["", "<sub>Posted by trio-test-harness (simulator CGM/pump, no real patient data).</sub>"]
    return "\n".join(lines), files


def post_to_pr(run: Run, pr: str, *, mode: str = "comment", repo: str | None = None,
               columns: int = 3, max_shots: int | None = None, artifact_url: str | None = None) -> str:
    """Comment on (or append to the description of) a PR with the run's screenshots attached inline."""
    from .build import resolve_pr
    slug, num = resolve_pr(pr) if not repo else (repo, int(pr))
    md, files = render_markdown(run, columns=columns, max_shots=max_shots, artifact_url=artifact_url)
    body = run.path / "pr-comment.md"
    body.write_text(md)
    attach = []
    for f in files:
        rel = f.relative_to(run.path)
        attach += ["--attach", f"./{rel}"]
    if mode == "comment":
        out = run(["gh", "pr", "comment", str(num), "--repo", slug, "--body-file", str(body), *attach],
                  cwd=run.path, timeout=900).stdout
        log(f"commented on {slug}#{num} with {len(files)} screenshots attached")
        return out.strip()
    if mode == "description":
        marker = "<!-- trio-test-harness -->"
        current = json.loads(_gh(["pr", "view", str(num), "--repo", slug, "--json", "body"], quiet=True))["body"] or ""
        new_body = (current.split(marker)[0].rstrip() if marker in current else current.rstrip()) + f"\n\n{marker}\n{md}\n"
        body.write_text(new_body)
        run(["gh", "pr", "edit", str(num), "--repo", slug, "--body-file", str(body), *attach], cwd=run.path, timeout=900)
        log(f"updated description of {slug}#{num}")
        return f"https://github.com/{slug}/pull/{num}"
    raise HarnessError(f"unknown mode {mode}")
