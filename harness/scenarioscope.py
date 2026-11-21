"""Which faketandem scenarios does this change need? `trio-test-e2e --pump-scenario auto` asks here.

    decide(info, src, base_branch, app) -> {"scenarios": [...], "reason": "...", "by": "classifier" | "paths"}

The run's PRs (harness/watchscope.py gathers them: titles, descriptions, changed files) go to a small model
(TH_SCOPE_MODEL, default haiku, through `claude -p`) with every preset's summary, and it picks 1 to rounds.AUTO_MAX of
them. Without Claude (no CLI, no login, an error or an unreadable answer) the changed paths decide: history or pump
clock code gets the reset and clock presets in HISTORY_PICK, anything else of the Tandem driver's gets GENERAL_PICK.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from .config import CONFIG
from .util import log, which

HISTORY_PATHS = re.compile(r"History|Backfill|Reconcil|Clock|TimeZone|DateChange|TimeChange|ShelfMode|ArmInit|"
                           r"BolusReportLedger|ReportedBasal|PumpEvent|HostDoseDates", re.I)
# a reset, a recorded change that ends on the phone's time, one past the trust limit, and the field log's pump
HISTORY_PICK = ["pump-reset", "clock-travel-12h-east", "clock-73h-ahead", "out-of-shelf-mode"]
GENERAL_PICK = ["recent-doses", "app-doses"]

PROMPT = """You choose the simulated pump histories for a UI test of a change to {app}, an iOS automated insulin \
delivery app, running against faketandem, an emulated Tandem Mobi pump driven by the app's TandemKit driver. Each \
scenario below replays hours of pump use (boluses, temp rates, suspends, pump resets, clock changes) into the pump \
before the app first connects. The test runs the app once per scenario you pick, from a clean app and pump each time, \
so every pick costs minutes: pick the 1 to {max} scenarios that would best show whether this change works or breaks, \
most telling first. A change to history reading, dose reporting, the pump clock or time zones wants the reset and \
clock scenarios that exercise it; a change elsewhere in the pump driver wants ordinary use (recent-doses, app-doses, \
suspended-now, ...); a change that cannot reach the pump driver wants just recent-doses.

Scenarios:
{scenarios}

Reply with one JSON object and nothing else: {{"scenarios": ["<name>", ...], "reason": "<one sentence>"}}

{prs}"""


def by_paths(entries: list[dict]) -> dict:
    files = [f for e in entries for f in e["files"] or []]
    hits = [f for f in files if HISTORY_PATHS.search(f)]
    if hits:
        return {"scenarios": list(HISTORY_PICK), "by": "paths",
                "reason": f"it changes history or pump clock code ({', '.join(hits[:3])}{', …' if len(hits) > 3 else ''})"}
    return {"scenarios": list(GENERAL_PICK), "by": "paths",
            "reason": "none of its changed files is history or pump clock code"}


def classify(entries: list[dict], app: str = "trio", *, model: str | None = None, timeout: float = 180) -> dict | None:
    from . import rounds
    from .faketandem import SCENARIOS
    from .watchscope import _claude_env, _describe
    claude = which("claude")
    if not claude:
        return None
    prompt = PROMPT.format(app="Loop" if app == "loop" else "Trio", max=rounds.AUTO_MAX,
                           scenarios="\n".join(f"- {k}: {v}" for k, v in SCENARIOS.items()), prs=_describe(entries))
    model = model or CONFIG.get("TH_SCOPE_MODEL") or "haiku"
    with tempfile.TemporaryDirectory() as tmp:   # no project files or skills for it to load
        try:
            cp = subprocess.run([claude, "-p", prompt, "--model", model, "--output-format", "json", "--max-turns", "1"],
                                cwd=tmp, env=_claude_env(), capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"scenario scope: the classifier did not answer ({e})")
            return None
    if cp.returncode != 0:
        log(f"scenario scope: the classifier failed: {(cp.stderr or cp.stdout).strip()[-300:]}")
        return None
    try:
        text = json.loads(cp.stdout).get("result", "")
        answer = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    except (ValueError, AttributeError):
        log(f"scenario scope: unreadable answer: {cp.stdout[-300:]}")
        return None
    return parse_answer(answer)


def parse_answer(answer: dict) -> dict | None:
    """The model's picks that are presets, in its order, without repeats, at most AUTO_MAX; None if none is."""
    from . import rounds
    from .faketandem import SCENARIOS
    picks = []
    for name in answer.get("scenarios") or []:
        if isinstance(name, str) and name in SCENARIOS and name not in picks:
            picks.append(name)
    if not picks:
        return None
    return {"scenarios": picks[:rounds.AUTO_MAX], "reason": str(answer.get("reason") or "").strip(), "by": "classifier"}


def decide(info: dict, src: Path, base_branch: str = "dev", app: str = "trio") -> dict:
    from .watchscope import gather
    entries = gather(info, src, base_branch)
    out = classify(entries, app) or by_paths(entries)
    out["prs"] = [e["pr"] for e in entries if e["pr"]]
    return out
