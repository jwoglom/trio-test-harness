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
from .util import run as run_cmd  # post_to_pr's `run` parameter shadows run()

CURRENT = REPO_ROOT / "runs" / ".current"


class Run:
    def __init__(self, path: Path):
        # Always absolute: `th shot --run runs/<dir>` (relative) used to log screenshots as `runs/<dir>/screenshots/…`,
        # which the report then failed to find under the run and drew as empty black cards.
        self.path = Path(path).expanduser().resolve()
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
            rec["screenshot"] = self.rel_shot(screenshot)
        rec.update(extra)
        with open(self.steps_path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        return rec

    def rel_shot(self, screenshot: Path | str) -> str:
        """Screenshot path as stored in steps.jsonl: relative to the run directory (`screenshots/001-x.png`)."""
        sp = Path(screenshot).expanduser()
        try:
            return str(sp.resolve().relative_to(self.path))
        except ValueError:
            return str(sp) if not sp.is_absolute() else f"screenshots/{sp.name}"

    def shot_file(self, step: dict) -> Path | None:
        """Resolve a step's screenshot to an existing file (run-relative, repo-relative or absolute), else None."""
        rel = step.get("screenshot")
        if not rel:
            return None
        for cand in (self.path / rel, REPO_ROOT / rel, Path(rel), self.shots / Path(rel).name):
            if cand.exists():
                return cand
        return None

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


VERDICT_LABEL = {"works": "✅ Works as expected", "broken": "❌ Not working as expected",
                 "inconclusive": "🟡 Inconclusive"}


def load_highlights(run: Run) -> dict | None:
    """The separate reviewer pass's verdict, prose and chosen screenshots (at most 10; ci/highlights.py), if any."""
    p = run.path / "highlights.json"
    try:
        doc = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    doc["highlights"] = [h for h in doc.get("highlights", []) if (run.path / h.get("screenshot", "")).is_file()]
    return doc if doc.get("summary") and doc["highlights"] else None


def _inline_md(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def _md_to_html(md: str) -> str:
    """Just enough markdown for the reviewer's prose: paragraphs, "- " bullets, **bold**, `code`."""
    out = []
    for block in re.split(r"\n\s*\n", md.strip()):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if lines and all(l.startswith(("- ", "* ")) for l in lines):
            out.append("<ul>" + "".join(f"<li>{_inline_md(l[2:])}</li>" for l in lines) + "</ul>")
        elif lines:
            out.append(f"<p>{_inline_md(' '.join(lines))}</p>")
    return "".join(out)


def _highlights_html(run: Run, hl: dict) -> str:
    figs = "".join(
        f'<figure><div class="frame"><img src="{_thumb_b64(run.path / h["screenshot"])}" '
        f'alt="{html.escape(h.get("caption", ""))}"></div><figcaption><span>{html.escape(h.get("caption", ""))}</span>'
        f'</figcaption></figure>'
        for h in hl["highlights"])
    return (f'<section class="highlights"><h2>{html.escape(VERDICT_LABEL.get(hl.get("verdict"), "Highlights"))}</h2>'
            f'<div class="prose">{_md_to_html(hl["summary"])}</div><div class="hl-grid">{figs}</div></section>'
            '<div class="spacer"></div><hr><div class="spacer"></div><h2>All steps</h2>')


def render_html(run: Run, out: Path | None = None) -> Path:
    meta = run.meta
    steps = shown_steps(run)
    title = meta.get("title") or f"Trio test: {meta.get('name','run')}"
    build = meta.get("build", {})
    git = build.get("git", {}) if isinstance(build, dict) else {}
    status = meta.get("status", "running")
    color = {"passed": "#1a7f37", "failed": "#cf222e"}.get(status, "#9a6700")
    cards = []
    for s in steps:
        caption = (f'<figcaption><b>{html.escape(s.get("action",""))}</b><span>{html.escape(s.get("note",""))}</span>'
                   f'<time>{html.escape(s.get("t","")[11:19])}</time></figcaption>')
        if (shot := run.shot_file(s)) is not None:
            img = f'<img src="{_thumb_b64(shot)}" alt="{html.escape(s.get("note",""))}" loading="lazy">'
            cards.append(f'<figure><div class="frame">{img}</div>{caption}</figure>')
        else:
            # Notes and other steps without a screenshot: text only, no empty phone frame.
            cards.append(f'<figure class="note">{caption}</figure>')
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
    if setup_note(run):
        facts.append(("Setup", setup_note(run)))
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
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:10px}}
.grid figcaption{{font-size:11px;padding:6px 7px}}
.highlights h2{{font-size:19px;margin:8px 0}} .prose p,.prose ul{{margin:0 0 10px}} .prose code{{font-size:13px}}
.hl-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:16px;margin-top:12px}}
.spacer{{height:24px}} hr{{border:0;border-top:1px solid var(--line);margin:0}} h2{{font-size:17px}}
figure{{margin:0;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}}
.frame{{aspect-ratio:9/19.5;background:#000;display:flex;align-items:center;justify-content:center}}
.frame img{{width:100%;height:100%;object-fit:contain}}
figure.note{{align-self:start;border-left:3px solid var(--line)}}
figcaption{{padding:8px 10px;font-size:13px;display:flex;flex-direction:column;gap:2px}} figcaption span{{color:var(--muted)}} time{{color:var(--muted);font-size:11px}}
</style></head><body><main>
<h1>{html.escape(title)}</h1><span class="status">{html.escape(status)}</span>
<dl>{fact_html}</dl>
<p class="summary">{html.escape(meta.get('summary',''))}</p>
{_highlights_html(run, hl) if (hl := load_highlights(run)) else ''}
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


def shown_steps(run: Run) -> list[dict]:
    """The steps a report shows: all of them, minus the routine setup when the run asked to hide it.

    `trio-test-e2e --hide-setup-screenshots` records meta["setup_steps"] (the step count once launch,
    skipped onboarding and the fake CGM/pump setup had succeeded); those steps are left out of
    reports. The screenshots stay in the run directory.
    """
    steps = run.steps()
    return steps[int(run.meta.get("setup_steps") or 0):]


def setup_note(run: Run) -> str | None:
    n = int(run.meta.get("setup_steps") or 0)
    return (f"{n} routine setup steps (launch, onboarding skipped, fake CGM + pump) are not shown; "
            "their screenshots are in the run artifact.") if n else None


def select_steps(run: Run, max_shots: int | None) -> list[dict]:
    """Steps with screenshots, evenly thinned to max_shots (first and last always kept)."""
    steps = [s for s in shown_steps(run) if run.shot_file(s) is not None]
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
    files: list[Path] = []
    hl = load_highlights(run)
    if hl:
        # The reviewer's pick: verdict, prose and the relevant screenshots; everything else stays in the run's report.
        lines += ["", f"#### {VERDICT_LABEL.get(hl.get('verdict'), 'Highlights')}", "", hl["summary"], ""]
        cells = []
        for h in hl["highlights"]:
            shot = run.path / h["screenshot"]
            files.append(shot)
            cap = html.escape(h.get("caption", ""))
            cells.append(f"![{cap}](./{h['screenshot']})<br><sub>{cap}</sub>")
        cols = min(columns, len(cells))
        lines += ["| " + " | ".join([" "] * cols) + " |", "|" + "---|" * cols]
        for i in range(0, len(cells), cols):
            row = cells[i:i + cols] + [" "] * (cols - len(cells[i:i + cols]))
            lines.append("| " + " | ".join(row) + " |")
        total = len([st for st in shown_steps(run) if st.get("screenshot")])
        where = f" in the [full report]({artifact_url})" if artifact_url else " in the run's report"
        lines += ["", f"<sub>{len(cells)} of {total} screenshots chosen by a separate review pass; all steps are{where}.</sub>",
                  "", "<sub>Posted by trio-test-harness (simulator CGM/pump, no real patient data).</sub>"]
        return "\n".join(lines), files
    steps = select_steps(run, max_shots)
    if setup_note(run):
        lines += ["", f"<sub>{setup_note(run)}</sub>"]
    if steps:
        total = len([s for s in shown_steps(run) if s.get("screenshot")])
        note = f" ({len(steps)} of {total} screenshots)" if len(steps) < total else ""
        lines += ["", f"<details open><summary>Screenshots{note}</summary>", ""]
        header = "| " + " | ".join([" "] * columns) + " |"
        lines += [header, "|" + "---|" * columns]
        for i in range(0, len(steps), columns):
            row = steps[i:i + columns]
            cells = []
            for s in row:
                shot = run.shot_file(s)
                rel = run.rel_shot(shot)
                files.append(shot)
                cap = html.escape(f"{s.get('action','')}: {s.get('note','')}".strip(": "))
                cells.append(f"![{cap}](./{rel})<br><sub>{cap}</sub>")
            cells += [" "] * (columns - len(row))
            lines.append("| " + " | ".join(cells) + " |")
        lines += ["", "</details>"]
    lines += ["", "<sub>Posted by trio-test-harness (simulator CGM/pump, no real patient data).</sub>"]
    return "\n".join(lines), files


def upload_assets(files: list[Path], token: str, repo: str) -> dict[Path, str]:
    """Upload images as GitHub comment attachments (user-attachments) with a user token.

    GitHub accepts these uploads only from user tokens, not App tokens, so CI uploads with the user's
    fine-grained PAT (scoped to `repo`) and posts the comment itself as the App. The assets belong to
    `repo`: they display for viewers who can read that repo. Returns {file: asset URL}.
    """
    import mimetypes
    import requests
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    r = requests.get(f"https://api.github.com/repos/{repo}", headers=hdrs, timeout=30)
    if r.status_code != 200:
        raise HarnessError(f"upload token cannot read {repo}: HTTP {r.status_code}")
    repo_id = r.json()["id"]
    urls: dict[Path, str] = {}
    for f in files:
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        with open(f, "rb") as fh:
            up = requests.post("https://uploads.github.com/user-attachments/assets",
                               params={"name": f.name, "content_type": ctype, "repository_id": repo_id},
                               headers={**hdrs, "Content-Type": "application/octet-stream"}, data=fh, timeout=120)
        if up.status_code >= 300 or not up.json().get("url"):
            raise HarnessError(f"uploading {f.name} failed: HTTP {up.status_code} {up.text[:200]}")
        urls[f] = up.json()["url"]
    return urls


def _with_uploaded_images(md: str, run: Run, urls: dict[Path, str]) -> str:
    for f, url in urls.items():
        md = md.replace(f"(./{f.relative_to(run.path)})", f"({url})")
    return md


def _without_images(md: str, artifact_url: str | None) -> str:
    """The comment body without its ![...](./screenshots/...) images; each cell keeps its caption."""
    text = re.sub(r"!\[[^\]]*\]\(\./screenshots/[^)]+\)(<br>)?", "", md).replace("<summary>Screenshots", "<summary>Steps")
    where = f"the run's artifact ({artifact_url})" if artifact_url else "the run's report"
    return text + f"\n\n_Screenshots could not be attached with this token; they are in {where}._\n"


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
        upload_token, upload_repo = CONFIG.get("TH_UPLOAD_TOKEN"), CONFIG.get("TH_UPLOAD_REPO")
        if upload_token and upload_repo and files:
            # CI: the images go up with a user token scoped to upload_repo; the comment itself is posted
            # with whatever GH_TOKEN is (the App), so the App is the visible author.
            try:
                body.write_text(_with_uploaded_images(md, run, upload_assets(files, upload_token, upload_repo)))
                attach = []
            except HarnessError as e:
                log(f"screenshot upload failed ({e}); posting without inline screenshots")
                body.write_text(_without_images(md, artifact_url))
                attach, files = [], []
        try:
            out = run_cmd(["gh", "pr", "comment", str(num), "--repo", slug, "--body-file", str(body), *attach],
                          cwd=run.path, timeout=900).stdout
        except HarnessError as e:
            # gh uploads --attach files as the signed-in user; a GitHub App token (CI) cannot, so post
            # the same comment with captions in place of the images.
            if "unsupported authentication type" not in str(e) or not attach:
                raise
            log("gh cannot attach files with this token; posting without inline screenshots")
            body.write_text(_without_images(md, artifact_url))
            out = run_cmd(["gh", "pr", "comment", str(num), "--repo", slug, "--body-file", str(body)],
                          cwd=run.path, timeout=300).stdout
            files = []
        log(f"commented on {slug}#{num} with {len(files)} screenshots attached")
        return out.strip()
    if mode == "description":
        marker = "<!-- trio-test-harness -->"
        current = json.loads(_gh(["pr", "view", str(num), "--repo", slug, "--json", "body"], quiet=True))["body"] or ""
        new_body = (current.split(marker)[0].rstrip() if marker in current else current.rstrip()) + f"\n\n{marker}\n{md}\n"
        body.write_text(new_body)
        run_cmd(["gh", "pr", "edit", str(num), "--repo", slug, "--body-file", str(body), *attach], cwd=run.path, timeout=900)
        log(f"updated description of {slug}#{num}")
        return f"https://github.com/{slug}/pull/{num}"
    raise HarnessError(f"unknown mode {mode}")
