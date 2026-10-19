"""Does this change need the Watch app in the test? `trio-test-e2e --devices auto` asks here.

    decide(info, src, app) -> {"devices": "phone" | "phone+watch", "reason": "...", "by": "classifier" | "paths",
                               "prs": [...], "files": N}

Every PR of the run counts: the app's (Trio, LoopWorkspace), the submodule PR it was given, other submodule PRs
(`--submodule <PR>`) and cherry-picked PRs. A small model (TH_SCOPE_MODEL, default haiku, through `claude -p`) reads their
titles, descriptions and changed files and answers phone or phone+watch. Without Claude (no CLI, no login, an error or an
unreadable answer) the changed paths decide: anything of the Watch app's or of the phone's side of the phone-watch link is
phone+watch. A wrong "phone" leaves the watch out of the test; a wrong "phone+watch" costs minutes of setup.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .config import CONFIG
from .util import HarnessError, log, run, which

# Trio: its Watch app and complication targets and the phone side of the link (AppleWatchManager, WatchMessageKeys, …).
# Loop: WatchApp / WatchApp Extension and its phone side (WatchDataManager, WatchContext).
WATCH_PATHS = re.compile(r"(^|/)Trio Watch |Watch ?Complication|WatchManager|WatchMessageKeys|WatchState|WatchLogger"
                         r"|(^|/)WatchApp( Extension)?/|WatchDataManager|WatchContext", re.I)
MAX_FILES = 400
MAX_BODY = 6000

APP_FACTS = {
    "trio": """Trio is an iOS automated insulin delivery app with an Apple Watch companion app. Its Watch app's code is under \
"Trio Watch App", "Trio Watch App Extension" and "Trio Watch Complication"; the phone's side of the link is \
AppleWatchManager, WatchMessageKeys and WatchState; the watch shows glucose, trend, IOB and COB and can enter boluses \
(with the phone's bolus safety checks), carbs, overrides and temp targets.""",
    "loop": """Loop is an iOS automated insulin delivery app (built from LoopWorkspace) with an Apple Watch companion app. Its \
Watch app's code is under "WatchApp" and "WatchApp Extension"; the phone's side of the link is WatchDataManager and \
WatchContext; the watch shows glucose, IOB, COB and the loop status and can enter carbs and boluses and set overrides.""",
}

PROMPT = """You decide which devices a UI test of a change needs. {facts} The test always runs the iPhone app in the iOS \
Simulator. Answer "phone+watch" when the change could alter anything the Watch app shows or does, or the data and \
messages passed between the phone and the watch, so the Watch app should also run on a paired watch simulator. That \
includes the Watch app's own code, the phone's side of the watch link, and phone features the watch displays or \
triggers. Answer "phone" when the change cannot reach the watch: phone-only screens, pump or CGM drivers, build files, \
tests and documentation. The change may span several pull requests (the app and its submodules); decide for all of \
them together.

Reply with one JSON object and nothing else: {{"devices": "phone" or "phone+watch", "reason": "<one sentence>"}}

{prs}"""


def run_prs(info: dict) -> list[tuple[str, str | None]]:
    """(owner/repo#N, submodule or None) for every PR the run tests, the main one first."""
    out: list[tuple[str, str | None]] = []
    if info.get("pr"):
        out.append((info["pr"], (info.get("submodule_pr") or {}).get("submodule")))
    out += [(o["pr"], o["name"]) for o in info.get("submodule_overrides") or [] if o.get("pr")]
    out += [(c["pr"]["pr"], c.get("submodule")) for c in (info.get("cherry_picks") or []) + (info.get("submodule_cherry_picks") or [])
            if (c.get("pr") or {}).get("pr")]
    seen, unique = set(), []
    for pr, sub in out:
        if pr not in seen:
            seen.add(pr)
            unique.append((pr, sub))
    return unique


def pr_details(pr: str) -> dict | None:
    slug, num = pr.split("#")
    try:
        return json.loads(run(["gh", "pr", "view", num, "--repo", slug, "--json", "title,body,files"], quiet=True,
                              timeout=120).stdout)
    except (HarnessError, ValueError):
        return None


def gather(info: dict, src: Path, base_branch: str) -> list[dict]:
    """[{"pr", "submodule", "title", "body", "files"}] for the run's PRs; for a branch or in-place build without one, the
    checkout's commits since the base branch."""
    entries = []
    for pr, sub in run_prs(info):
        d = pr_details(pr) or {}
        files = [f["path"] for f in d.get("files") or []] if d else None
        entries.append({"pr": pr, "submodule": sub, "title": d.get("title") or "", "body": d.get("body") or "",
                        "files": [f"{sub}/{f}" if sub else f for f in files] if files is not None else None})
    if entries:
        return entries
    files = None
    for base in (f"origin/{base_branch}", f"upstream/{base_branch}"):
        cp = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=src, capture_output=True, text=True)
        if cp.returncode == 0:
            files = [line for line in cp.stdout.splitlines() if line]
            break
    return [{"pr": None, "submodule": None, "title": info.get("describe") or info.get("branch") or "",
             "body": "", "files": files}]


def by_paths(entries: list[dict]) -> dict:
    files = [f for e in entries for f in e["files"] or []]
    hits = [f for f in files if WATCH_PATHS.search(f)]
    if hits:
        return {"devices": "phone+watch", "reason": f"it changes Watch-related files ({', '.join(hits[:3])}"
                + (", …" if len(hits) > 3 else "") + ")", "by": "paths"}
    unread = any(e["files"] is None for e in entries)
    why = "some changed files could not be read and none read is the Watch app's or the phone-watch link's" if unread \
        else "none of its changed files is the Watch app's or the phone-watch link's"
    return {"devices": "phone", "reason": why, "by": "paths"}


def _claude_env() -> dict:
    # a fresh top-level `claude -p`, also when the pipeline itself runs inside Claude Code
    return {k: v for k, v in os.environ.items()
            if k != "CLAUDECODE" and not (k.startswith("CLAUDE_CODE_") and k != "CLAUDE_CODE_OAUTH_TOKEN")}


def _describe(entries: list[dict]) -> str:
    parts = []
    budget = MAX_FILES
    for e in entries:
        head = f"Pull request {e['pr']}" + (f" (submodule {e['submodule']})" if e["submodule"] else "") if e["pr"] \
            else "Commits since the base branch"
        files = e["files"]
        shown = (files or [])[:max(budget, 20)]
        budget -= len(shown)
        parts.append(f"## {head}\nTitle: {e['title'] or '(none)'}\n\nDescription:\n{(e['body'] or '(none)')[:MAX_BODY]}\n\n"
                     f"Changed files ({'unknown' if files is None else len(files)}"
                     f"{', first ' + str(len(shown)) + ' shown' if files and len(shown) < len(files) else ''}):\n"
                     + ("\n".join(shown) or "(none)"))
    return "\n\n".join(parts)


def classify(entries: list[dict], app: str = "trio", *, model: str | None = None, timeout: float = 180) -> dict | None:
    """The model's answer, or None when it could not be had."""
    claude = which("claude")
    if not claude:
        return None
    prompt = PROMPT.format(facts=APP_FACTS.get(app, APP_FACTS["trio"]), prs=_describe(entries))
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


def decide(info: dict, src: Path, base_branch: str = "dev", app: str = "trio") -> dict:
    entries = gather(info, src, base_branch)
    out = classify(entries, app) or by_paths(entries)
    out["prs"] = [e["pr"] for e in entries if e["pr"]]
    files = [e["files"] for e in entries]
    out["files"] = None if all(f is None for f in files) else sum(len(f or []) for f in files)
    return out
