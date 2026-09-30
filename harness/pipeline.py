"""`trio-test-e2e`: one entry point for the whole workflow, usable with flags or interactively.

    ./trio-test-e2e --mode simulator --pr https://github.com/nightscout/Trio/pull/1234
    ./trio-test-e2e --mode vnc --pr https://github.com/jwoglom/TandemKit/pull/395     # submodule PR
    ./trio-test-e2e                                                                    # prompts for everything

Stages (each also available on its own via `./trio-test-harness ...` and as a Claude skill):
  checkout -> build -> install -> launch (+onboarding) -> smoke (home + fake CGM/pump) -> report -> [PR comment]
The exploratory part — reading the PR description and exercising what it changes — is done by Claude with the
`trio-test-e2e` skill; this script prepares everything, writes runs/<run>/pr.md with the PR description and a test-plan
stub, and can hand off to Claude with --agent.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import CONFIG, REPO_ROOT
from .util import HarnessError, log

MODES = {"simulator": "sim", "sim": "sim",
         "rooted-vnc": "rooted-vnc", "vnc": "rooted-vnc", "device": "rooted-vnc",
         "iphone-usb": "iphone-usb", "mirror": "iphone-usb"}


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(prompt: str, default: str | None = None, choices: list[str] | None = None) -> str:
    if not _tty():
        if default is not None:
            return default
        raise HarnessError(f"missing input ({prompt}) and no terminal to prompt on")
    suffix = f" [{default}]" if default else ""
    if choices:
        suffix = f" ({'/'.join(choices)}){suffix}"
    while True:
        ans = input(f"{prompt}{suffix}: ").strip()
        if not ans and default is not None:
            return default
        if ans and (not choices or ans in choices):
            return ans


def _confirm(prompt: str, default: bool = False) -> bool:
    if not _tty():
        return default
    ans = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not ans else ans in ("y", "yes")


def stage_checkout(args) -> tuple[Path, dict]:
    from . import build as B
    log("== stage: checkout")
    src, info = B.prepare_source(branch=args.branch, pr=args.pr, in_place=Path(args.in_place) if args.in_place else None,
                                 submodules=args.submodule, fetch=not args.no_fetch, trio_branch=args.trio_branch,
                                 cherry_picks=args.cherry_pick, submodule_cherry_picks=args.submodule_cherry_pick)
    return src, info


def write_pr_notes(run, info: dict) -> Path:
    """runs/<run>/pr.md: PR title + description + what the agent should test. Also printed."""
    lines = [f"# {info.get('pr_title') or info.get('pr') or info.get('branch') or 'in-place build'}", ""]
    if info.get("pr_url"):
        lines.append(f"PR: {info['pr_url']}")
    if info.get("submodule_pr"):
        t = info["submodule_pr"]
        lines.append(f"Submodule PR: {t['slug']}#{t['number']} on submodule `{t['submodule']}` (Trio @ {t['trio_branch']})")
    for c in info.get("cherry_picks") or []:
        lines.append(f"Cherry-picked onto Trio: {c['what']}: {c['applied']} commit(s)"
                     + (f", {c['dropped']} already there" if c["dropped"] else ""))
    for c in info.get("submodule_cherry_picks") or []:
        lines.append(f"Cherry-picked onto submodule {c['submodule']}: {c['what']}: {c['applied']} commit(s)"
                     + (f", {c['dropped']} already there" if c["dropped"] else ""))
    lines += [f"Commit: {info.get('sha','?')} {info.get('describe','')}", ""]
    if info.get("pr_body"):
        lines += ["## PR description", "", info["pr_body"].strip(), ""]
    for c in (info.get("cherry_picks") or []) + (info.get("submodule_cherry_picks") or []):
        if pr := c.get("pr"):
            lines += [f"## Cherry-picked PR {pr['pr']}: {pr.get('title') or ''}", "", f"{pr.get('url') or ''}", "",
                      (pr.get("body") or "(no description)").strip(), ""]
    lines += ["## Test plan (fill in from the description)", "",
              "1. Smoke: launch, home screen, Glucose Simulator + Pump Simulator (automated by trio-test-e2e).",
              "2. Exercise every user-visible change the description mentions; screenshot before/after each.",
              "3. Note anything that looks wrong with `th run note`.", ""]
    p = run.path / "pr.md"
    p.write_text("\n".join(lines))
    return p


def stage_build(args, src: Path, info: dict):
    from . import build as B
    log("== stage: build")
    # rooted-vnc: unsigned + fake-signed IPA for AppSync; iphone-usb: real automatic signing (TH_DEVELOPER_TEAM) for devicectl
    return B.build(src, "sim" if args.target == "sim" else "device", configuration=args.configuration, clean=args.clean,
                   git_info=info, trollstore=(args.target == "rooted-vnc"))


def stage_install(args, bi):
    from .cli import get_driver
    log("== stage: install")
    if args.target == "sim":
        drv = get_driver("sim"); drv.boot()
        drv.uninstall(bi.bundle_id) if args.reinstall else None
        drv.install(Path(bi.app_path))
    elif args.target == "rooted-vnc":
        from . import devices as D
        D.appsync_install(Path(bi.ipa_path), CONFIG.get("TH_DEVICE_UDID") or None)
    else:
        from . import devices as D
        D.devicectl_install(D.pick_udid(), Path(bi.app_path))


def stage_launch(args, bi, run):
    """Launch Trio with onboarding pre-seeded (or not), dismiss the update alert, confirm the home screen."""
    from .cli import get_driver
    from .flows import trio as F
    log("== stage: launch")
    drv = get_driver(args.target)
    if args.target == "sim":
        drv.boot()
        if args.onboarding == "preseed":
            F.preseed_onboarding(drv.app_data_container(bi.bundle_id))
        drv.launch(bi.bundle_id)
        from . import wda as W
        if not W.is_running():
            W.start(drv.udid)
    elif args.target == "rooted-vnc":
        from . import devices as D
        drv.unlock()
        flags = {"crashlyticsSharingEnabled": False, "telemetrySharingEnabled": False}
        if args.onboarding == "preseed":
            flags.update({"onboardingCompleted": True, "hasSeenFatProteinOrderChange": True})
        D.ssh_preseed_flags(bi.bundle_id, flags)
        D.ssh_kill("Trio")
        D.ssh_launch(bi.bundle_id)
    else:  # iphone-usb: no filesystem access → onboarding must be walked; launch via Spotlight in the mirror
        drv.boot()
        drv.open_app_via_spotlight("Trio")
        if args.onboarding == "preseed":
            log("iphone-usb cannot pre-seed onboarding (no root); walking it instead")
            args.onboarding = "walk"
    time.sleep(6)
    F.step(run, drv, "launch", "Trio launched")
    F.settle(drv, run)
    if args.onboarding == "walk" and not F.at_home(drv):
        F.walk_onboarding(drv, run)
    F.settle(drv, run)
    if not F.at_home(drv):
        F.step(run, drv, "not-home", "Home screen not detected after launch")
        raise HarnessError("Trio did not reach the home screen; see the run screenshots")
    F.step(run, drv, "home", "Home screen")
    return drv


def smoke_devices(args) -> list[str]:
    """What the smoke stage sets up: the Glucose Simulator and/or the Pump Simulator."""
    if args.no_smoke:
        return []
    return [n for n, off in (("Glucose Simulator", args.no_cgm_sim), ("Pump Simulator", args.no_pump_sim)) if not off]


def stage_smoke(args, drv, run):
    from .flows import trio as F
    want = smoke_devices(args)
    log(f"== stage: smoke ({' + '.join(want)})")
    F.settle(drv, run)
    if F.devices_configured(drv):
        run.log_step("devices-present", "CGM and pump already configured from a previous install (app data kept); skipping setup")
        F.step(run, drv, "home-with-devices", "Home with devices already configured")
        return
    if "Glucose Simulator" in want and drv.find("Add CGM", exact=True):
        F.add_cgm_simulator(drv, run)
        time.sleep(1.5)
    if "Pump Simulator" in want and drv.find("Add pump", exact=True):
        F.add_pump_simulator(drv, run)
    time.sleep(4)
    F.settle(drv, run)
    F.step(run, drv, "home-with-devices", f"Home with {' + '.join(want)}")


def stage_report(args, run) -> Path:
    from .report import render_html
    log("== stage: report")
    return render_html(run)


def stage_comment(args, run, pr_url: str | None) -> None:
    from .report import post_to_pr
    if not pr_url:
        return
    if args.comment == "never":
        return
    if args.comment == "ask" and not _confirm(f"Post a comment with the report on {pr_url}?", default=False):
        return
    print(post_to_pr(run, pr_url, mode="comment", artifact_url=args.artifact_url))


def hand_off_to_agent(run, pr_url: str | None, mode: str) -> None:
    claude = shutil.which("claude")
    if not claude:
        raise HarnessError("claude CLI not found on PATH")
    prompt = (f"Use the trio-test-e2e skill. A Trio run has already been prepared by `trio-test-e2e`: run dir {run.path} "
              f"(mode {mode}{', PR ' + pr_url if pr_url else ''}). The app is installed, launched, on the home screen with "
              f"fake CGM/pump configured. Read {run.path}/pr.md, exercise what the PR description says with `./trio-test-harness` commands, "
              f"screenshot each step into this run, finish the run, publish report.html as an Artifact, and ask me whether "
              f"to post the PR comment (./trio-test-harness report pr --artifact-url ...).")
    os.execv(claude, [claude, prompt])


def run_stages(args):
    """checkout -> build -> install -> launch -> smoke -> report. Returns (exit code, run, PR URL, pr.md)."""
    from .report import Run
    from .build import BuildInfo
    src, info = stage_checkout(args)
    pr_url = info.get("pr_url")
    # a branch with a cherry-picked PR on top is a test of that PR (its title, its comment)
    picked = next((c["pr"] for c in (info.get("cherry_picks") or []) + (info.get("submodule_cherry_picks") or [])
                   if c.get("pr")), None)
    if picked and not info.get("pr"):
        pr_url = pr_url or picked.get("url")
        info.setdefault("pr_title", f"{picked.get('title')} (on {info.get('branch')})")
    if args.checkout_only:
        print(json.dumps(info, indent=2, default=str)); return 0, None, None, None
    name = (f"pr-{info['pr'].split('#')[-1]}" if info.get("pr") else (info.get("branch") or "inplace")).replace("/", "-")
    if info.get("cherry_picks") or info.get("submodule_cherry_picks"):
        name += "-picks"
    run = Run.new(f"{name}-{args.target}", {"target": args.target, "mode": args.mode,
                                             "title": f"Trio test: {info.get('pr_title') or info.get('branch') or 'in-place'} ({args.mode})",
                                             "pr_url": pr_url})
    notes = write_pr_notes(run, info)
    print(f"\n--- {notes} ---\n" + notes.read_text() + "\n---\n")
    try:
        bi = BuildInfo.load() if args.skip_build else stage_build(args, src, info)
        run.update_meta(build=bi.__dict__)
        if not args.skip_install:
            stage_install(args, bi)
        drv = stage_launch(args, bi, run)
        if smoke_devices(args):
            first = len(run.steps())
            stage_smoke(args, drv, run)
            if args.hide_setup_screenshots:
                # Only the fake CGM/pump setup is hidden; launch, home and everything after stay in the report.
                run.update_meta(hidden_steps=[first, len(run.steps())])
        want = smoke_devices(args)
        run.finish("passed", "Trio launched, reached the home screen" + (f", {' + '.join(want)} configured" if want else "") + ".")
    except Exception as e:  # noqa: BLE001
        run.finish("failed", f"{type(e).__name__}: {e}")
        stage_report(args, run)
        log(f"FAILED: {e}")
        print(f"run: {run.path}")
        return 1, run, pr_url, notes
    html = stage_report(args, run)
    print(f"\nrun:    {run.path}\nreport: {html}\npr.md:  {notes}")
    return 0, run, pr_url, notes


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="trio-test-e2e", description="End-to-end Trio test: checkout → build → install → smoke → report")
    p.add_argument("--mode", choices=sorted(MODES),
                   help="simulator | rooted-vnc (jailbroken iPhone: TrollVNC + SSH + AppSync) | iphone-usb (any iPhone via "
                        "macOS iPhone Mirroring; needs a signed build + devicectl)")
    p.add_argument("--pr", help="Trio PR or submodule PR (URL, owner/repo#N)")
    p.add_argument("--branch", help="Trio branch instead of a PR: feat/x (origin, else upstream), upstream:feat/x, "
                                      "owner/repo:feat/x")
    p.add_argument("--trio-branch", help="Trio branch to test a submodule PR against (default: derived from .gitmodules)")
    from .build import CHERRY_PICK_HELP, SUBMODULE_CHERRY_PICK_HELP
    p.add_argument("--cherry-pick", action="append", metavar="SPEC", help=CHERRY_PICK_HELP)
    p.add_argument("--submodule-cherry-pick", action="append", metavar="SPEC", help=SUBMODULE_CHERRY_PICK_HELP)
    p.add_argument("--submodule", action="append", default=[], help="extra NAME@REF overrides")
    p.add_argument("--in-place", help="build this checkout as-is")
    p.add_argument("--configuration", default="Debug")
    p.add_argument("--clean", action="store_true"); p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--reinstall", action="store_true", default=True)
    p.add_argument("--onboarding", choices=["preseed", "walk"], default="preseed")
    p.add_argument("--skip-build", action="store_true", help="reuse build/last-build.json")
    p.add_argument("--skip-install", action="store_true")
    p.add_argument("--no-smoke", action="store_true", help="stop after reaching the home screen")
    p.add_argument("--no-cgm-sim", action="store_true", help="smoke: do not add the Glucose Simulator")
    p.add_argument("--no-pump-sim", action="store_true", help="smoke: do not add the Pump Simulator")
    p.add_argument("--hide-setup-screenshots", action="store_true",
                   help="leave the fake CGM/pump setup steps out of report.html and the PR comment once they "
                        "succeeded (kept in the run directory)")
    p.add_argument("--checkout-only", action="store_true")
    p.add_argument("--comment", choices=["ask", "auto", "never"], default="ask")
    p.add_argument("--artifact-url", help="claude.ai artifact link to include in the PR comment")
    p.add_argument("--agent", dest="agent", action="store_true", default=None,
                   help="after the smoke test, open Claude Code with the trio-test-e2e skill (default when run from a terminal "
                        "outside Claude Code and `claude` is installed)")
    p.add_argument("--no-agent", dest="agent", action="store_false")
    p.add_argument("--yes", "-y", action="store_true", help="no prompts; defaults everywhere")
    args = p.parse_args(argv)

    if not args.mode:
        args.mode = "simulator" if args.yes else _ask("Target", default="simulator", choices=["simulator", "rooted-vnc", "iphone-usb"])
    args.target = MODES[args.mode]
    if not args.pr and not args.branch and not args.in_place and not args.yes:
        ans = _ask("PR URL (Trio or submodule), branch name, or blank for the current checkout", default="")
        if ans.startswith("http") or "#" in ans:
            args.pr = ans
        elif ans:
            args.branch = ans

    # The device lock (harness/busy.py) says who drives the target; `th busy` and the web UI read it. Released
    # before the hand-off below, so an interactive session left open afterwards never holds the device.
    from contextlib import nullcontext
    from . import busy
    what = f"trio-test-e2e {args.pr or args.branch or 'in-place'}" + (f" + cherry-picks" if args.cherry_pick else "")
    with nullcontext() if args.checkout_only else busy.hold(args.target, what, mode=args.mode):
        rc, run, pr_url, notes = run_stages(args)
    if rc or run is None:
        return rc
    inside_claude = bool(os.environ.get("CLAUDECODE"))
    if args.agent is None:
        args.agent = (not inside_claude) and _tty() and shutil.which("claude") is not None
    if args.agent and not inside_claude:
        stage_comment(args, run, pr_url) if args.comment == "auto" else None
        hand_off_to_agent(run, pr_url, args.mode)   # execs claude; the PR-comment question is asked there
    stage_comment(args, run, pr_url)
    print("\nNext (Claude / trio-test-e2e skill): read pr.md, exercise the described changes with ./trio-test-harness, then publish "
          "report.html as an Artifact and `./trio-test-harness report pr <PR> --artifact-url <link>` if the user agrees.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
