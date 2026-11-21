"""Pump scenario rounds: one build tested against several faketandem scenarios in one run.

    trio-test-e2e --pump faketandem --pump-scenario pump-reset,clock-fall-back,clock-73h-ahead --pump-setup walk
    trio-test-e2e --pump faketandem --pump-scenario auto

`--pump-scenario` takes one preset (or a JSON file), a comma-separated list of them, or `auto`, for which a small model
picks up to AUTO_MAX presets from the run's PRs (harness/scenarioscope.py). With more than one, each is a round: the
pump and the app start clean for every round, so nothing a driver read or reported for one scenario leaks into the next.

Round 1 is prepared by trio-test-e2e as a single scenario would be. The job (CI, the web UI) then runs its agent once
per round, and between two runs `trio-test-harness rounds next` prepares the next round outside the agent's sandbox:
it saves the pump's side of the round into the run (faketandem/<n>-<scenario>/), starts a new faketandem with the next
scenario, reinstalls the app (an empty data container: no doses, no pump manager state), seeds it as trio-test-e2e
does and gets it to its home screen with the simulated CGM. A job's agent cannot run it (harness/jobguard.py): it
replaces what the job set up. An interactive session handed the run by trio-test-e2e can, and is told to.

run.json's `rounds` holds the plan ({"scenario", "setup"} per round), the current round (0-based), how the plan was
chosen, and the trio-test-e2e options a later round is prepared with. Every round's steps sit in the one run, after a
`round` step that names its scenario.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .util import HarnessError, log

MAX_ROUNDS = 6
AUTO_MAX = 4
# trio-test-e2e's options that preparing a later round needs; the rest (checkout, build) is done once.
SAVED_ARGS = ("mode", "target", "onboarding", "devices", "watch", "watch_model", "watch_wrist", "watch_crown",
              "watch_text_size", "pump", "pump_setup", "faketandem_ref", "no_smoke", "no_cgm_sim", "no_pump_sim",
              "hide_setup_screenshots", "configuration")


def parse(spec: str | None) -> list[str] | str | None:
    """None for no scenario, "auto", or the presets/files named, in order."""
    if not spec or spec == "none":
        return None
    if spec.strip() == "auto":
        return "auto"
    names = [s.strip() for s in spec.split(",") if s.strip()]
    if not names:
        return None
    if "auto" in names:
        raise HarnessError("--pump-scenario auto cannot be combined with named scenarios")
    if len(names) > MAX_ROUNDS:
        raise HarnessError(f"--pump-scenario names {len(names)} scenarios; one run takes at most {MAX_ROUNDS}")
    if len(set(names)) != len(names):
        raise HarnessError("--pump-scenario names a scenario twice")
    from .faketandem import check_scenario
    for n in names:
        check_scenario(n)
    return names


def plan(names: list[str], setup: str, *, walk_history: bool = False) -> list[dict]:
    """One round per scenario, on `setup`; with `walk_history` (auto), walk for the presets that test history dating,
    which a preseeded driver never reports (faketandem.HISTORY_SCENARIOS)."""
    from .faketandem import HISTORY_SCENARIOS
    return [{"scenario": n, "setup": "walk" if walk_history and n in HISTORY_SCENARIOS else setup} for n in names]


def preseed_warnings(rounds: list[dict]) -> list[str]:
    from .faketandem import HISTORY_SCENARIOS
    return [f"scenario {r['scenario']} tests how history is dated, which a preseeded pump never reports (nothing from "
            "before the driver first ran) and whose clock setup never sets; use --pump-setup walk"
            for r in rounds if r["scenario"] in HISTORY_SCENARIOS and r["setup"] == "preseed"]


def saved_args(args) -> dict:
    return {k: getattr(args, k) for k in SAVED_ARGS if hasattr(args, k)}


def state(run_dir) -> dict | None:
    """run.json's `rounds`, None for a run of one scenario or none."""
    try:
        r = json.loads((Path(run_dir) / "run.json").read_text()).get("rounds")
    except (OSError, ValueError):
        return None
    return r if r and len(r.get("plan") or []) > 1 else None


def count(run_dir) -> int:
    r = state(run_dir)
    return len(r["plan"]) if r else 1


def round_dir_name(index: int, scenario: str) -> str:
    return f"{index + 1}-{re.sub(r'[^a-zA-Z0-9._-]+', '-', Path(scenario).stem if Path(scenario).is_file() else scenario)}"


def faketandem_save_dir(run_dir) -> Path:
    """Where the pump's side of the current round goes: faketandem/ for a run of one scenario, faketandem/<n>-<name>/
    for each round of several."""
    base = Path(run_dir) / "faketandem"
    r = state(run_dir)
    if not r:
        return base
    i = r["current"]
    return base / round_dir_name(i, r["plan"][i]["scenario"])


def log_round(run, rounds: dict) -> None:
    i, p = rounds["current"], rounds["plan"]
    r = p[i]
    run.log_step("round", f"Scenario {i + 1} of {len(p)}: {r['scenario']} "
                          f"({'pre-paired' if r['setup'] == 'preseed' else 'set up in the app'})",
                 round=i + 1, scenario=r["scenario"], pump_setup=r["setup"])


def next_round(run_dir) -> dict:
    """Prepares the next round of `run_dir` (see the module docstring). Returns the round prepared."""
    from . import faketandem as FT
    from . import pipeline as P
    from .build import BuildInfo
    from .report import Run
    run = Run(Path(run_dir))
    rounds = (run.meta or {}).get("rounds")
    if not rounds or len(rounds.get("plan") or []) < 2:
        raise HarnessError(f"{run.path} is a run of one pump scenario; there is no next round")
    i = rounds["current"] + 1
    if i >= len(rounds["plan"]):
        raise HarnessError(f"{run.path}: all {len(rounds['plan'])} rounds have run")
    nxt = rounds["plan"][i]
    args = argparse.Namespace(**rounds["args"])
    args.pump_scenario, args.pump_setup = nxt["scenario"], nxt["setup"]
    args.reinstall, args.skip_install = True, False      # an empty data container: nothing of the last round
    bi = BuildInfo.load()
    if bi is None:
        raise HarnessError("no build to reinstall (build/last-build.json)")
    args.faketandem_reuse = FT.current()     # its binary, so the next one is not fetched and built again
    if args.faketandem_reuse:
        FT.stop(save_to=run.path)     # into the finished round's faketandem/<n>-<scenario>/
    rounds["current"] = i
    run.update_meta(rounds=rounds, setup_problems=[], status="running")
    log(f"== round {i + 1} of {len(rounds['plan'])}: {nxt['scenario']} ({nxt['setup']})")
    log_round(run, rounds)
    P.stage_install(args, bi)
    drv = P.stage_launch(args, bi, run)
    problems: list[dict] = []
    try:
        P.stage_home(args, drv, run)
    except Exception as e:  # noqa: BLE001 - the agent can finish it, as after trio-test-e2e
        P.setup_problem(run, drv, problems, "reaching the home screen", e)
    if P.smoke_devices(args):
        try:
            P.stage_smoke(args, drv, run, bi, problems)
        except Exception as e:  # noqa: BLE001
            P.setup_problem(run, drv, problems, "the smoke stage", e)
    run.finish("partial" if problems else "passed",
               f"round {i + 1} ({nxt['scenario']}) prepared" + (": " + "; ".join(p["what"] for p in problems)
                                                                 if problems else ""))
    return nxt


def agent_notes(run_dir, *, job: bool = True) -> str:
    """What the agent is told about the rounds: which one it tests and what comes after. Empty for one scenario."""
    r = state(run_dir)
    if not r:
        return ""
    from .faketandem import SCENARIOS
    i, p = r["current"], r["plan"]
    cur = p[i]
    lines = [f"\n## Pump scenario {i + 1} of {len(p)}: {cur['scenario']}", "",
             f"This run tests the same build against {len(p)} faketandem scenarios, one after another, each from a "
             "new pump and a reinstalled app: " + ", ".join(
                 f"{'**' if k == i else ''}{x['scenario']}{'**' if k == i else ''}" for k, x in enumerate(p)) + ".",
             f"This pass is scenario {i + 1}: {SCENARIOS.get(cur['scenario'], cur['scenario'])}.",
             f"The pump is {'already paired' if cur['setup'] == 'preseed' else 'not set up yet: walk its setup'}."]
    if r.get("reason"):
        lines.append(f"The scenarios were chosen {r.get('by')}: {r['reason']}")
    lines.append("")
    lines.append("Test what the change under test means for this scenario: what the pump did before the app "
                 "connected is in `faketandem history`. Start each step's note with the scenario's name, so the report "
                 "tells the rounds apart.")
    if i + 1 < len(p):
        lines.append(("When you are done with this scenario, end your session; the job then prepares the next one."
                      if job else "When you are done with this scenario, run `./trio-test-harness rounds next` to "
                      "prepare the next one (it reinstalls the app and restarts faketandem), then test it."))
    else:
        lines.append("This is the last scenario; finish the run as usual, summing up every scenario's result.")
    return "\n".join(lines) + "\n"


RANK = {"passed": 0, "partial": 1, "failed": 2}


def finish(run, status: str, summary: str) -> None:
    """`run finish` in a run of rounds: the verdict goes on the current round, and the run's status is the worst of
    the rounds finished so far, its summary each one's, so a later round's pass does not hide an earlier failure."""
    rounds = run.meta["rounds"]
    i = rounds["current"]
    rounds["plan"][i]["result"] = {"status": status, "summary": summary}
    done = [(k, x) for k, x in enumerate(rounds["plan"]) if x.get("result")]
    worst = max((x["result"]["status"] for _, x in done), key=lambda st: RANK.get(st, 1))
    run.update_meta(rounds=rounds)
    run.finish(worst, " ".join(f"[{k + 1}. {x['scenario']}: {x['result']['status']}] {x['result']['summary']}".strip()
                               for k, x in done))


def combine_summaries(run_dir) -> Path | None:
    """agent-summary.md from each round's agent-summary-<n>.md, under a heading per scenario."""
    run_dir = Path(run_dir)
    r = state(run_dir)
    if not r:
        return None
    parts = []
    for k, x in enumerate(r["plan"]):
        f = run_dir / f"agent-summary-{k + 1}.md"
        if f.exists():
            parts.append(f"## Scenario {k + 1}: {x['scenario']}\n\n{f.read_text().strip()}\n")
        else:
            parts.append(f"## Scenario {k + 1}: {x['scenario']}\n\n(no agent summary: this round did not run)\n")
    out = run_dir / "agent-summary.md"
    out.write_text("\n".join(parts))
    return out
