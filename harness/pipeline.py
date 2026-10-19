"""`trio-test-e2e`: one entry point for the whole workflow, usable with flags or interactively.

    ./trio-test-e2e --mode simulator --pr https://github.com/nightscout/Trio/pull/1234
    ./trio-test-e2e --mode vnc --pr https://github.com/jwoglom/TandemKit/pull/395     # submodule PR
    ./trio-test-e2e --app loop --pr https://github.com/jwoglom/TandemKit/pull/395     # the same PR in Loop
    ./trio-test-e2e                                                                    # prompts for everything

`--app loop` (or TH_APP=loop) tests Loop, built from LoopWorkspace, instead of Trio (harness/apps.py): the same stages
with Loop's checkout, scheme and UI flows (harness/flows/loop.py).

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


def _app():
    from . import apps
    return apps.current()


def stage_checkout(args) -> tuple[Path, dict]:
    from . import build as B
    log(f"== stage: checkout ({_app().name})")
    src, info = B.prepare_source(branch=args.branch, pr=args.pr, in_place=Path(args.in_place) if args.in_place else None,
                                 submodules=args.submodule, fetch=not args.no_fetch, trio_branch=args.trio_branch,
                                 cherry_picks=args.cherry_pick, submodule_cherry_picks=args.submodule_cherry_pick)
    return src, info


def decide_devices(args, info: dict, src: Path) -> dict:
    """Settle --devices auto: the watch only for Trio on the simulator, and then when the change can reach it."""
    if args.devices != "auto":
        return {"devices": args.devices, "reason": "asked for", "by": "option"}
    if args.target != "sim":
        return {"devices": "phone", "reason": "the watch runs only beside the iPhone simulator", "by": "mode"}
    from . import watchscope
    app = _app()
    d = watchscope.decide(info, src, app.base_branch, app.key)
    log(f"devices: {d['devices']} ({d['by']}: {d['reason']})")
    if d["devices"] == "phone+watch" and app.key != "trio":
        d["reason"] += f" — but the harness runs only Trio's Watch app, so this {app.name} run is phone-only"
        d["devices"] = "phone"
    args.watch = d["devices"] == "phone+watch"
    return d


def write_pr_notes(run, info: dict) -> Path:
    """runs/<run>/pr.md: PR title + description + what the agent should test. Also printed."""
    app = _app()
    F = app.flows
    lines = [f"# {info.get('pr_title') or info.get('pr') or info.get('branch') or 'in-place build'}", ""]
    lines.append(f"App: {app.name} ({app.default_repo})")
    if info.get("pr_url"):
        lines.append(f"PR: {info['pr_url']}")
    if info.get("submodule_pr"):
        t = info["submodule_pr"]
        lines.append(f"Submodule PR: {t['slug']}#{t['number']} on submodule `{t['submodule']}` ({app.default_repo} @ {t['trio_branch']})")
    for c in info.get("cherry_picks") or []:
        lines.append(f"Cherry-picked onto {app.name}: {c['what']}: {c['applied']} commit(s)"
                     + (f", {c['dropped']} already there" if c["dropped"] else ""))
    for c in info.get("submodule_cherry_picks") or []:
        lines.append(f"Cherry-picked onto submodule {c['submodule']}: {c['what']}: {c['applied']} commit(s)"
                     + (f", {c['dropped']} already there" if c["dropped"] else ""))
    lines += [f"Commit: {info.get('sha','?')} {info.get('describe','')}"]
    if d := info.get("devices"):
        lines.append(f"Devices: {d['devices'].replace('+', ' + ')} ({'chosen' if d['by'] == 'option' else d['by'] + ': ' + d['reason']})")
    lines.append("")
    if info.get("pr_body"):
        lines += ["## PR description", "", info["pr_body"].strip(), ""]
    for c in (info.get("cherry_picks") or []) + (info.get("submodule_cherry_picks") or []):
        if pr := c.get("pr"):
            lines += [f"## Cherry-picked PR {pr['pr']}: {pr.get('title') or ''}", "", f"{pr.get('url') or ''}", "",
                      (pr.get("body") or "(no description)").strip(), ""]
    lines += ["## Test plan (fill in from the description)", "",
              f"1. Smoke: launch, home screen, {F.CGM_SIMULATOR} + {F.PUMP_SIMULATOR} (automated by trio-test-e2e).",
              "2. Exercise every user-visible change the description mentions; screenshot before/after each.",
              "3. Note anything that looks wrong with `th run note`.", ""]
    p = run.path / "pr.md"
    p.write_text("\n".join(lines))
    return p


def stage_build(args, src: Path, info: dict):
    from . import build as B
    log(f"== stage: build ({_app().name})")
    # rooted-vnc: unsigned + fake-signed IPA for AppSync; iphone-usb: real automatic signing (TH_DEVELOPER_TEAM) for devicectl
    return B.build(src, "sim" if args.target == "sim" else "device", configuration=args.configuration, clean=args.clean,
                   git_info=info, trollstore=(args.target == "rooted-vnc"))


def stage_install(args, bi):
    from .cli import get_driver
    log("== stage: install")
    if args.target == "sim":
        if args.watch:
            # The watch model's own phone + watch pair (harness/watch.py ensure_pair): from here on every command
            # drives that phone.
            from . import watch as WT
            WT.pair(args.watch_model)
        drv = get_driver("sim"); drv.boot()
        if args.watch and args.reinstall:
            get_driver("watch").uninstall(bi.bundle_id)     # before the phone app, whose uninstall would delete it too
        drv.uninstall(bi.bundle_id) if args.reinstall else None
        drv.install(Path(bi.app_path))
        _app().flows.reset_after_install(drv, bi.bundle_id) if args.reinstall else None
        if args.watch:
            # after the phone app: the Watch app installed first was never linked to it
            get_driver("watch").install(Path(bi.app_path))
    elif args.target == "rooted-vnc":
        from . import devices as D
        D.appsync_install(Path(bi.ipa_path), CONFIG.get("TH_DEVICE_UDID") or None)
    else:
        from . import devices as D
        D.xcode_install(Path(bi.app_path))


def stage_launch(args, bi, run):
    """Launch the app with onboarding pre-seeded (or not), clear what it shows first, confirm the home screen."""
    from .cli import get_driver
    app = _app()
    F = app.flows
    log(f"== stage: launch ({app.name})")
    if args.target == "sim" and args.watch:
        from . import watch as WT
        WT.pair(args.watch_model)
    drv = get_driver(args.target)
    if args.target == "sim":
        drv.boot()
        if args.onboarding == "preseed":
            F.preseed_simulator(drv, bi.bundle_id)
        if args.watch:
            # The Watch app goes first: Trio's phone session checks for it once, when the phone app starts.
            from . import watch as WT
            udid = WT.watch_udid()
            WT.write_settings(udid, wrist=args.watch_wrist, crown=args.watch_crown, text_size=args.watch_text_size)
            WT.start(udid)
            get_driver("watch").launch(bi.bundle_id)
        drv.launch(bi.bundle_id)
        from . import wda as W
        W.start(drv.udid)       # moves it when it runs on another phone (another watch pair)
    elif args.target == "rooted-vnc":
        from . import devices as D
        drv.unlock()
        if not F.preseed_phone(bi.bundle_id, onboarding=args.onboarding == "preseed") and args.onboarding == "preseed":
            log(f"{app.name} cannot be pre-seeded on a phone; walking its onboarding instead")
            args.onboarding = "walk"
        D.ssh_kill(app.name)
        D.ssh_launch(bi.bundle_id)
    else:  # iphone-usb: no filesystem access → onboarding must be walked; launch via Spotlight in the mirror
        drv.boot()
        drv.open_app_via_spotlight(app.name)
        if args.onboarding == "preseed":
            log("iphone-usb cannot pre-seed onboarding (no root); walking it instead")
            args.onboarding = "walk"
    time.sleep(6)
    F.step(run, drv, "launch", f"{app.name} launched")
    F.settle(drv, run)
    if (args.onboarding == "walk" or F.PROMPTS_AFTER_PRESEED) and not F.at_home(drv):
        F.walk_onboarding(drv, run)
    F.settle(drv, run)
    if not F.at_home(drv):
        F.step(run, drv, "not-home", "Home screen not detected after launch")
        raise HarnessError(f"{app.name} did not reach the home screen; see the run screenshots")
    F.step(run, drv, "home", "Home screen")
    if getattr(args, "watch", False):
        F.step(run, get_driver("watch"), "watch-home", "Watch app on the paired watch simulator")
    return drv


def smoke_devices(args) -> list[str]:
    """What the smoke stage sets up: the app's simulated CGM and/or pump (Trio: Glucose Simulator, Loop: CGM Simulator;
    both: Pump Simulator)."""
    if args.no_smoke:
        return []
    F = _app().flows
    return [n for n, off in ((F.CGM_SIMULATOR, args.no_cgm_sim), (F.PUMP_SIMULATOR, args.no_pump_sim)) if not off]


def stage_smoke(args, drv, run, bi=None):
    app = _app()
    F = app.flows
    want = smoke_devices(args)
    log(f"== stage: smoke ({' + '.join(want)})")
    F.settle(drv, run)
    if F.devices_configured(drv):
        run.log_step("devices-present", "CGM and pump already configured from a previous install (app data kept); skipping setup")
        F.step(run, drv, "home-with-devices", "Home with devices already configured")
        return
    if F.CGM_SIMULATOR in want and F.offers(drv, F.ADD_CGM):
        F.add_cgm_simulator(drv, run)
        time.sleep(1.5)
        if args.target == "sim" and bi and F.give_cgm_glucose(drv, bi.bundle_id):
            run.log_step("cgm-glucose", f"gave the {F.CGM_SIMULATOR} a sine-curve glucose model and relaunched {app.name}")
            time.sleep(6)
            F.settle(drv, run)
    if F.PUMP_SIMULATOR in want and F.offers(drv, F.ADD_PUMP):
        F.add_pump_simulator(drv, run)
    time.sleep(4)
    F.settle(drv, run)
    F.step(run, drv, "home-with-devices", f"Home with {' + '.join(want)}")
    if getattr(args, "watch", False):
        from .cli import get_driver
        w = get_driver("watch")
        # the phone pushes its state to the watch on its next loop; "STALE DATA" until then
        for _ in range(12):
            if not w.find("STALE DATA"):
                break
            time.sleep(5)
        F.step(run, w, "watch-with-data", "Watch app after the phone's fake CGM and pump are set up")


def stage_report(args, run) -> Path:
    from .report import render_html
    log("== stage: report")
    collect_crashes(args, run)
    return render_html(run)


def collect_crashes(args, run) -> None:
    """The app's crash reports since the run started go into the run's crashes/, one step each (best effort)."""
    from . import appdebug, apps
    from .build import BuildInfo
    from .cli import norm_target
    target = norm_target(args.mode)
    if target not in ("sim", "rooted-vnc"):
        return
    app = apps.current()
    try:
        if target == "sim":
            from . import appdb
            from .cli import get_driver
            bundle = appdb.installed_bundle(get_driver("sim"), app)
        else:
            bi = BuildInfo.load(app)
            bundle = bi.bundle_id if bi else app.bundle_id(CONFIG.team_for("device"))
        for r in appdebug.save_crashes(run, target, app.name, bundle):
            log(f"{app.name} crashed during the run: {run.path / 'crashes' / r['name']}")
    except Exception as e:  # noqa: BLE001 - the report must still render
        log(f"crash reports not collected: {e}")


def stage_comment(args, run, pr_url: str | None) -> None:
    from .report import post_to_pr
    if not pr_url:
        return
    if args.comment == "never":
        return
    if args.comment == "ask" and not _confirm(f"Post a comment with the report on {pr_url}?", default=False):
        return
    print(post_to_pr(run, pr_url, mode="comment", artifact_url=args.artifact_url))


def hand_off_to_agent(run, pr_url: str | None, mode: str, *, watch: bool = False) -> None:
    claude = shutil.which("claude")
    if not claude:
        raise HarnessError("claude CLI not found on PATH")
    app = _app()
    prompt = (f"Use the trio-test-e2e skill. A {app.name} run has already been prepared by `trio-test-e2e` (--app {app.key}; "
              f"TH_APP={app.key} is set, so every ./trio-test-harness command acts on {app.name}): run dir {run.path} "
              f"(mode {mode}{', PR ' + pr_url if pr_url else ''}). The app is installed, launched, on the home screen with "
              f"fake CGM/pump configured. Read {run.path}/pr.md, exercise what the PR description says with `./trio-test-harness` commands, "
              f"screenshot each step into this run, finish the run, publish report.html as an Artifact, and ask me whether "
              f"to post the PR comment (./trio-test-harness report pr --artifact-url ...)."
              + (" A paired watch simulator runs the Watch app with the input agent up: drive it with "
                 "`./trio-test-harness --target watch shot|tap-text|find|tree|crown` (trio-ui-drive skill, Watch section)."
                 if watch else ""))
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
    info["devices"] = decide_devices(args, info, src)
    name = (f"pr-{info['pr'].split('#')[-1]}" if info.get("pr") else (info.get("branch") or "inplace")).replace("/", "-")
    if info.get("cherry_picks") or info.get("submodule_cherry_picks"):
        name += "-picks"
    app = _app()
    if app.key != "trio":
        name = f"{app.key}-{name}"
    run = Run.new(f"{name}-{args.target}", {"target": args.target, "mode": args.mode, "app": app.key, "watch": args.watch,
                                             "devices": info.get("devices"),
                                             "title": f"{app.name} test: {info.get('pr_title') or info.get('branch') or 'in-place'} ({args.mode})",
                                             "pr_url": pr_url})
    notes = write_pr_notes(run, info)
    print(f"\n--- {notes} ---\n" + notes.read_text() + "\n---\n")
    try:
        bi = BuildInfo.load() if args.skip_build else stage_build(args, src, info)
        if bi is None:
            raise HarnessError(f"--skip-build: no earlier {app.name} build ({app.last_build.name})")
        run.update_meta(build=bi.__dict__)
        if not args.skip_install:
            stage_install(args, bi)
        drv = stage_launch(args, bi, run)
        if smoke_devices(args):
            first = len(run.steps())
            stage_smoke(args, drv, run, bi)
            if args.hide_setup_screenshots:
                # Only the fake CGM/pump setup is hidden; launch, home and everything after stay in the report.
                run.update_meta(hidden_steps=[first, len(run.steps())])
        want = smoke_devices(args)
        run.finish("passed", f"{app.name} launched, reached the home screen" + (f", {' + '.join(want)} configured" if want else "") + ".")
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
    from . import apps
    p = argparse.ArgumentParser(prog="trio-test-e2e", description="End-to-end Trio or Loop test: checkout → build → install → smoke → report")
    p.add_argument("--app", choices=sorted(apps.APPS),
                   help="the app under test: trio (nightscout/Trio) or loop (LoopKit/LoopWorkspace); default $TH_APP, else trio")
    p.add_argument("--mode", choices=sorted(MODES),
                   help="simulator | rooted-vnc (jailbroken iPhone: TrollVNC + SSH + AppSync) | iphone-usb (any iPhone via "
                        "macOS iPhone Mirroring; needs a signed build + devicectl)")
    p.add_argument("--pr", help="a PR on the app's repo or one of its submodules (URL, owner/repo#N); Loop's own repo "
                                "(LoopKit/Loop) is a LoopWorkspace submodule")
    p.add_argument("--branch", help="branch of the app's repo instead of a PR: feat/x (origin, else upstream), "
                                    "upstream:feat/x, owner/repo:feat/x")
    p.add_argument("--trio-branch", help="branch of the app's repo (Trio, LoopWorkspace) to test a submodule PR against "
                                         "(default: derived from .gitmodules)")
    from .build import CHERRY_PICK_HELP, SUBMODULE_CHERRY_PICK_HELP
    p.add_argument("--cherry-pick", action="append", metavar="SPEC", help=CHERRY_PICK_HELP)
    p.add_argument("--submodule-cherry-pick", action="append", metavar="SPEC", help=SUBMODULE_CHERRY_PICK_HELP)
    p.add_argument("--submodule", action="append", default=[], help="extra NAME@REF overrides")
    p.add_argument("--in-place", help="build this checkout as-is")
    p.add_argument("--configuration", default="Debug")
    p.add_argument("--clean", action="store_true"); p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--reinstall", action="store_true", default=True)
    p.add_argument("--onboarding", choices=["preseed", "walk"], default="preseed")
    p.add_argument("--devices", choices=["phone", "phone+watch", "auto"], default="phone",
                   help="phone (default): the iPhone app only. phone+watch: also a watch simulator paired with the "
                        "phone, the app's Watch app on it and the watch input agent, for `th --target watch ...` "
                        "(simulator, Trio). auto: a small model decides from every PR of the run (the app's and its "
                        "submodules': titles, descriptions, changed files; harness/watchscope.py)")
    p.add_argument("--watch", dest="devices", action="store_const", const="phone+watch", help="= --devices phone+watch")
    p.add_argument("--watch-model", metavar="MODEL", help="watch size: 40mm, 41mm, 42mm, 44mm, 45mm, 46mm, 49mm (or "
                                                          "ultra), or a model name from `th watch model` (default "
                                                          "TH_WATCH_SIM_DEVICE)")
    p.add_argument("--watch-wrist", choices=["left", "right"])
    p.add_argument("--watch-crown", choices=["left", "right"], help="the side the Digital Crown is on")
    p.add_argument("--watch-text-size", metavar="SIZE", help="watch text size (`th watch settings --help`)")
    p.add_argument("--skip-build", action="store_true", help="reuse build/last-build.json")
    p.add_argument("--skip-install", action="store_true")
    p.add_argument("--no-smoke", action="store_true", help="stop after reaching the home screen")
    p.add_argument("--no-cgm-sim", action="store_true", help="smoke: do not add the Glucose Simulator (Loop: CGM Simulator)")
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
    p.add_argument("--git-protocol", choices=["ssh", "https"],
                   help="how git reaches GitHub: ssh (key in ssh-agent) or https (gh CLI login); default $TH_GIT_PROTOCOL or ssh")
    args = p.parse_args(argv)
    app = apps.select(args.app)
    if args.git_protocol:
        CONFIG.set("TH_GIT_PROTOCOL", args.git_protocol)

    if not args.mode:
        args.mode = "simulator" if args.yes else _ask("Target", default="simulator", choices=["simulator", "rooted-vnc", "iphone-usb"])
    args.target = MODES[args.mode]
    if args.devices == "phone+watch" and (args.target != "sim" or app.key != "trio"):
        raise HarnessError("--devices phone+watch needs --mode simulator and Trio: the watch is a simulator paired with "
                           "the iPhone simulator, and the Watch flows are Trio's")
    args.watch = args.devices == "phone+watch"     # auto is settled after the checkout (run_stages)
    if args.watch_model and args.target == "sim":
        from . import watch as WT
        args.watch_model = WT.resolve_model(args.watch_model)    # a typo fails now, not after the build
    if not args.pr and not args.branch and not args.in_place and not args.yes:
        ans = _ask(f"PR URL ({app.name} or submodule), branch name, or blank for the current checkout", default="")
        if ans.startswith("http") or "#" in ans:
            args.pr = ans
        elif ans:
            args.branch = ans

    # The device lock (harness/busy.py) says who drives the target; `th busy` and the web UI read it. Released
    # before the hand-off below, so an interactive session left open afterwards never holds the device.
    from contextlib import nullcontext
    from . import busy
    what = f"trio-test-e2e{'' if app.key == 'trio' else ' --app ' + app.key} {args.pr or args.branch or 'in-place'}" \
        + (" + cherry-picks" if args.cherry_pick else "")
    with nullcontext() if args.checkout_only else busy.hold(args.target, what, mode=args.mode):
        rc, run, pr_url, notes = run_stages(args)
    if rc or run is None:
        return rc
    inside_claude = bool(os.environ.get("CLAUDECODE"))
    if args.agent is None:
        args.agent = (not inside_claude) and _tty() and shutil.which("claude") is not None
    if args.agent and not inside_claude:
        stage_comment(args, run, pr_url) if args.comment == "auto" else None
        hand_off_to_agent(run, pr_url, args.mode, watch=args.watch)   # execs claude; the PR-comment question is asked there
    stage_comment(args, run, pr_url)
    print("\nNext (Claude / trio-test-e2e skill): read pr.md, exercise the described changes with ./trio-test-harness, then publish "
          "report.html as an Artifact and `./trio-test-harness report pr <PR> --artifact-url <link>` if the user agrees.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
