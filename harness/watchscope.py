"""Does this change need the Watch app in the test? `trio-test-e2e --devices auto` (the default) asks here.

    decide(info, src) -> {"devices": "phone" | "phone+watch", "reason": "...", "by": "classifier" | "paths"}

A small model (TH_SCOPE_MODEL, default haiku, through `claude -p`) reads the PR's title, description and changed files
and answers phone or phone+watch. Without Claude (no CLI, no login, an error or an unreadable answer) the changed paths
decide: anything of the Watch app's or of the phone's side of the phone-watch link is phone+watch. Nothing that is
decided here can make a run unsafe: a wrong "phone" leaves the watch out, a wrong "phone+watch" costs a minute of setup.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .config import CONFIG
from .util import HarnessError, log, run

# Trio's Watch app and complication targets, and the phone side of the link (AppleWatchManager, WatchMessageKeys, …)
WATCH_PATHS = re.compile(r"(^|/)Trio Watch |Watch ?Complication|WatchManager|WatchMessageKeys|WatchState|WatchLogger",
                         re.I)
MAX_FILES = 400
MAX_BODY = 8000

PROMPT = """You decide which devices a UI test of a change to Trio needs. Trio is an iOS automated insulin delivery \
app with an Apple Watch companion app. The test always runs the iPhone app in the iOS Simulator; answer "phone+watch" \
when the change could alter anything the Watch app shows or does, or the data and messages passed between the phone \
and the watch, so the Watch app should also run on a paired watch simulator. That includes the Watch app's own code \
(paths under "Trio Watch App", "Trio Watch App Extension", "Trio Watch Complication"), the phone's watch link \
(AppleWatchManager, WatchMessageKeys, WatchState) and phone features the watch displays or triggers (glucose, trend, \
IOB, COB, bolus and carb entry, bolus safety checks, overrides, temp targets). Answer "phone" when the change cannot \
reach the watch: phone-only screens, pump or CGM drivers, build files, tests and documentation.

Reply with one JSON object and nothing else: {{"devices": "phone" or "phone+watch", "reason": "<one sentence>"}}

Title: {title}

Description:
{body}

Changed files ({nfiles}{more}):
{files}
"""


def _pr_files(slug_num: str) -> list[str] | None:
    slug, num = slug_num.split("#")
    try:
        meta = json.loads(run(["gh", "pr", "view", num, "--repo", slug, "--json", "files"], quiet=True,
                              timeout=120).stdout)
        return [f["path"] for f in meta.get("files") or []]
    except (HarnessError, ValueError, KeyError):
        return None


def changed_files(info: dict, src: Path, base_branch: str) -> list[str] | None:
    """Paths the change touches: the PR's (a submodule PR's are prefixed with the submodule), any cherry-picked PRs',
    else the checkout's commits since the base branch. None when nothing could be read."""
    files: list[str] = []
    found = False
    prs = [(info["pr"], (info.get("submodule_pr") or {}).get("submodule"))] if info.get("pr") else []
    prs += [(c["pr"]["pr"], c.get("submodule")) for c in (info.get("cherry_picks") or []) + (info.get("submodule_cherry_picks") or [])
            if (c.get("pr") or {}).get("pr")]
    for pr, sub in prs:
        got = _pr_files(pr)
        if got is not None:
            found = True
            files += [f"{sub}/{f}" if sub else f for f in got]
    if found:
        return files
    for base in (f"origin/{base_branch}", f"upstream/{base_branch}"):
        cp = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=src, capture_output=True, text=True)
        if cp.returncode == 0:
            return [line for line in cp.stdout.splitlines() if line]
    return None


def by_paths(files: list[str] | None) -> dict:
    hits = [f for f in files or [] if WATCH_PATHS.search(f)]
    if hits:
        return {"devices": "phone+watch", "reason": f"it changes Watch-related files ({', '.join(hits[:3])}"
                + (", …" if len(hits) > 3 else "") + ")", "by": "paths"}
    why = "none of its changed files is the Watch app's or the phone-watch link's" if files else \
        "its changed files could not be read"
    return {"devices": "phone", "reason": why, "by": "paths"}


def _claude_env() -> dict:
    # a fresh top-level `claude -p`, also when the pipeline itself runs inside Claude Code
    return {k: v for k, v in os.environ.items()
            if k != "CLAUDECODE" and not (k.startswith("CLAUDE_CODE_") and k != "CLAUDE_CODE_OAUTH_TOKEN")}


def classify(title: str, body: str, files: list[str] | None, *, model: str | None = None,
             timeout: float = 180) -> dict | None:
    """The model's answer, or None when it could not be had."""
    from .util import which
    claude = which("claude")
    if not claude:
        return None
    files = files or []
    prompt = PROMPT.format(title=title or "(none)", body=(body or "(none)")[:MAX_BODY], nfiles=len(files),
                           more=f", first {MAX_FILES} shown" if len(files) > MAX_FILES else "",
                           files="\n".join(files[:MAX_FILES]) or "(unknown)")
    model = model or CONFIG.get("TH_SCOPE_MODEL") or "haiku"
    with tempfile.TemporaryDirectory() as tmp:   # no project files or skills for it to load
        try:
            cp = subprocess.run([claude, "-p", prompt, "--model", model, "--output-format", "json", "--max-turns", "1"],
                                cwd=tmp, env=_claude_env(), capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"device scope: the classifier did not answer ({e})")
            return None
    if cp.returncode != 0:
        log(f"device scope: the classifier failed: {(cp.stderr or cp.stdout).strip()[-300:]}")
        return None
    try:
        text = json.loads(cp.stdout).get("result", "")
        answer = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    except (ValueError, AttributeError):
        log(f"device scope: unreadable answer: {cp.stdout[-300:]}")
        return None
    if answer.get("devices") not in ("phone", "phone+watch"):
        return None
    return {"devices": answer["devices"], "reason": str(answer.get("reason") or "").strip(), "by": "classifier"}


def decide(info: dict, src: Path, base_branch: str = "dev") -> dict:
    files = changed_files(info, src, base_branch)
    title = info.get("pr_title") or info.get("describe") or info.get("branch") or ""
    body = info.get("pr_body") or ""
    for c in (info.get("cherry_picks") or []) + (info.get("submodule_cherry_picks") or []):
        if pr := c.get("pr"):
            body += f"\n\nCherry-picked PR {pr.get('pr')}: {pr.get('title') or ''}\n{pr.get('body') or ''}"
    out = classify(title, body, files) or by_paths(files)
    out["files"] = len(files) if files is not None else None
    return out
