"""`th` command line: one entry point for everything the Claude skills call."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from .config import CONFIG, ENV_FILE, REPO_ROOT
from .util import tmp_path, HarnessError, log, run, which, port_open


def get_driver(target: str):
    if target == "sim":
        from .sim import SimulatorDriver
        return SimulatorDriver()
    if target == "device":
        from .vnc import VNCDriver
        return VNCDriver()
    raise HarnessError(f"unknown target {target!r} (sim|device)")


def _target(args) -> str:
    return getattr(args, "target", None) or os.environ.get("TH_TARGET") or "sim"


def _run(args):
    from .report import Run
    return Run.current(getattr(args, "run", None))


# ---------------- doctor -----------------

def cmd_doctor(args):
    from . import xcode
    ok = True

    def item(name, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"  [{'ok' if good else 'MISSING'}] {name}{': ' + detail if detail else ''}")

    print("Xcode")
    app = xcode.find_xcode()
    item("Xcode.app", app, f"{app} (v{xcode.xcode_version(app)})" if app else "run ./bootstrap-xcode.sh")
    if app:
        try:
            rts = xcode.list_runtimes()
            item("iOS simulator runtime", rts, ", ".join(r["name"] for r in rts) or "th sim install-runtime")
        except HarnessError as e:
            item("simctl", False, str(e)[:200])
    print("Tools")
    for t in ("idb", "idb_companion", "iproxy", "tesseract", "gh", "git", "xcodes"):
        item(t, which(t), which(t) or "")
    try:
        import vncdotool, pymobiledevice3  # noqa: F401
        item("python deps (vncdotool, pymobiledevice3)", True)
    except ImportError as e:
        item("python deps", False, str(e))
    print("Trio source")
    item("TRIO_REPO", CONFIG.trio_repo.exists(), str(CONFIG.trio_repo))
    print("Physical iPhone")
    try:
        from .devices import usbmux_devices
        devs = usbmux_devices()
        item("usbmuxd device", devs, "; ".join(f"{d.get('name','?')} iOS {d.get('ios','?')} ({d['connection']}) {d['udid']}" for d in devs) or "none connected")
    except HarnessError as e:
        item("usbmuxd", False, str(e))
    vnc_host = "127.0.0.1" if CONFIG.bool("TH_VNC_VIA_USB") else CONFIG.get("TH_VNC_HOST")
    vnc_port = int(CONFIG.get("TH_VNC_LOCAL_PORT") if CONFIG.bool("TH_VNC_VIA_USB") else CONFIG.get("TH_VNC_PORT"))
    print(f"  [info] TrollVNC target {vnc_host}:{vnc_port} ({'iproxy over USB' if CONFIG.bool('TH_VNC_VIA_USB') else 'network'})"
          f"{' — port currently open' if port_open(vnc_host, vnc_port) else ''}")
    print("GitHub")
    cp = run(["gh", "auth", "status"], check=False, quiet=True)
    item("gh auth", cp.returncode == 0)
    print("Config")
    item("harness.env", ENV_FILE.exists(), str(ENV_FILE) if ENV_FILE.exists() else "cp harness.env.example harness.env")
    item("TH_DEVELOPER_TEAM (device signing)", CONFIG.get("TH_DEVELOPER_TEAM") or None,
         CONFIG.get("TH_DEVELOPER_TEAM") or "not set: device builds use --trollstore or fail")
    free = shutil.disk_usage("/").free // 2**30
    item("disk free", free > 15, f"{free} GB")
    print("\nsummary:", "all good" if ok else "some items missing (see above)")
    return 0 if ok else 1


# ---------------- build / install / launch -----------------

def cmd_build(args):
    from . import build as B
    src, info = B.prepare_source(branch=args.branch, pr=args.pr, repo=args.repo,
                                 in_place=Path(args.in_place) if args.in_place else None,
                                 submodules=args.submodule, fetch=not args.no_fetch)
    if args.checkout_only:
        print(json.dumps(info, indent=2)); return 0
    bi = B.build(src, _target(args), configuration=args.configuration, clean=args.clean, git_info=info,
                 trollstore=args.trollstore)
    print(json.dumps(bi.__dict__, indent=2))
    return 0


def cmd_install(args):
    from .build import BuildInfo
    bi = BuildInfo.load()
    app = Path(args.app) if args.app else (Path(bi.app_path) if bi else None)
    if not app or not app.exists():
        raise HarnessError("no built app; run `th build` first or pass --app")
    target = _target(args)
    if target == "sim":
        drv = get_driver("sim"); drv.boot()
        if args.reinstall and bi:
            drv.uninstall(bi.bundle_id)
        drv.install(app)
        log(f"installed {app.name} on simulator {drv.udid}")
        return 0
    # device
    from . import devices as D
    if args.trollstore or (bi and bi.target == "device-trollstore"):
        ipa = Path(args.ipa) if args.ipa else (Path(bi.ipa_path) if bi and bi.ipa_path else None)
        if not ipa or not ipa.exists():
            from .build import package_ipa
            ipa = package_ipa(app, CONFIG.build_dir / "Trio.ipa")
        url, httpd = D.trollstore_install_url(ipa)
        print(f"\nTrollStore install link (open on the phone):\n  {url}\n")
        if args.open_on_device:
            drv = get_driver("device")
            drv.wake()
            log("opening the link in Safari on the phone via VNC")
            drv.open_app_via_spotlight("Safari")
            time.sleep(1.5)
            w, h = drv.screen_size()
            drv.tap(w // 2, int(h * 0.93))          # focus the Safari address bar (bottom bar layout)
            time.sleep(0.8)
            drv.type_text(url + "\n")
            print("Now confirm 'Open in TrollStore' and 'Install' on the phone (th shot / th tap-text).")
        print(f"serving {ipa} for {args.serve_seconds}s ... (Ctrl-C to stop)")
        try:
            time.sleep(args.serve_seconds)
        except KeyboardInterrupt:
            pass
        httpd.shutdown()
        return 0
    udid = D.pick_udid()
    D.devicectl_install(udid, app)
    log(f"installed on device {udid} via devicectl")
    return 0


def cmd_launch(args):
    from .build import BuildInfo
    bi = BuildInfo.load()
    bundle = args.bundle_id or (bi.bundle_id if bi else CONFIG.bundle_id(_target(args)))
    target = _target(args)
    if target == "sim":
        drv = get_driver("sim"); drv.boot()
        if args.preseed_onboarding:
            from .flows.trio import preseed_onboarding
            if not drv.is_installed(bundle):
                raise HarnessError(f"{bundle} not installed on the simulator; run `th install` first")
            paths = preseed_onboarding(drv.app_data_container(bundle))
            log(f"pre-seeded onboarding flag: {[str(p) for p in paths]}")
        drv.launch(bundle)
        return 0
    from . import devices as D
    if args.via == "devicectl":
        D.devicectl_launch(D.pick_udid(), bundle)
    else:
        drv = get_driver("device"); drv.wake(); drv.open_app_via_spotlight(args.app_name)
    return 0


# ---------------- sim / device utilities -----------------

def cmd_sim(args):
    from . import xcode
    sub = args.sub
    if sub == "list":
        for d in xcode.list_devices():
            print(f"{d['udid']}  {d['state']:9} {d['name']}  {d['runtimeIdentifier'].split('.')[-1]}")
    elif sub == "runtimes":
        for r in xcode.list_runtimes():
            print(f"{r['identifier']}  {r['name']}")
    elif sub == "install-runtime":
        cmd = ["xcodebuild", "-downloadPlatform", "iOS"]
        if args.version:
            cmd += ["-buildVersion", args.version]
        run(cmd, env=xcode.developer_env(), capture=False, timeout=7200)
    elif sub == "boot":
        drv = get_driver("sim"); drv.boot(); print(drv.udid)
    elif sub == "shutdown":
        d = xcode.ensure_simulator(); xcode.xcrun(["simctl", "shutdown", d["udid"]], check=False)
    elif sub == "erase":
        d = xcode.ensure_simulator(); xcode.xcrun(["simctl", "shutdown", d["udid"]], check=False)
        xcode.xcrun(["simctl", "erase", d["udid"]])
    elif sub == "container":
        from .build import BuildInfo
        bi = BuildInfo.load(); drv = get_driver("sim")
        print(drv.app_data_container(args.bundle_id or bi.bundle_id))
    elif sub == "defaults":
        from .build import BuildInfo
        bi = BuildInfo.load(); drv = get_driver("sim")
        drv.defaults_write(args.bundle_id or bi.bundle_id, args.key, args.value, args.type)
    elif sub == "openurl":
        drv = get_driver("sim"); drv.open_url(args.url)
    return 0


def cmd_device(args):
    from . import devices as D
    if args.sub == "list":
        for d in D.usbmux_devices():
            print(json.dumps(d))
        from . import xcode
        if xcode.find_xcode():
            for d in xcode.devicectl_devices():
                print("devicectl:", d.get("deviceProperties", {}).get("name"), d.get("hardwareProperties", {}).get("udid"),
                      d.get("connectionProperties", {}).get("pairingState"))
    elif args.sub == "vnc-check":
        drv = get_driver("device")
        w, h = drv.screen_size()
        print(f"connected; framebuffer {w}x{h}")
        p = Path(args.out or str(tmp_path("vnc-check.png"))); drv.screenshot(p); print(p)
    elif args.sub == "serve-ipa":
        url, httpd = D.trollstore_install_url(Path(args.ipa))
        print(url)
        try:
            time.sleep(args.serve_seconds)
        except KeyboardInterrupt:
            pass
        httpd.shutdown()
    elif args.sub == "wake":
        get_driver("device").wake()
    return 0


# ---------------- interaction primitives -----------------

def cmd_shot(args):
    drv = get_driver(_target(args))
    if args.out:
        p = drv.screenshot(Path(args.out))
    else:
        from .report import Run
        try:
            r = Run.current(args.run)
        except HarnessError:
            r = Run.new("adhoc", {"target": _target(args)})
        p = r.shot_path(args.name or "shot")
        drv.screenshot(p)
        r.log_step(args.name or "shot", args.note or "", p)
    print(p)
    if args.ocr:
        from .ocr import dump_text
        print(dump_text(p))
    return 0


def cmd_tap(args):
    get_driver(_target(args)).tap(args.x, args.y, duration=args.duration); return 0


def cmd_swipe(args):
    get_driver(_target(args)).swipe(args.x1, args.y1, args.x2, args.y2, duration=args.duration); return 0


def cmd_scroll(args):
    get_driver(_target(args)).scroll(args.direction, args.amount); return 0


def cmd_type(args):
    get_driver(_target(args)).type_text(args.text); return 0


def cmd_key(args):
    get_driver(_target(args)).key(args.name); return 0


def cmd_home(args):
    get_driver(_target(args)).home(); return 0


def cmd_tap_text(args):
    drv = get_driver(_target(args))
    x, y = drv.tap_text(args.text, exact=args.exact, index=args.index)
    print(f"tapped {args.text!r} at {x},{y}")
    return 0


def cmd_find(args):
    drv = get_driver(_target(args))
    nodes = drv.find(args.text, exact=args.exact)
    for n in nodes:
        print(json.dumps(n.to_dict()))
    if not nodes:
        from .ocr import find_text
        p = drv.screenshot(tmp_path("find.png"))
        for h in find_text(p, args.text):
            print(json.dumps({"ocr": h.text, "cx": h.center[0], "cy": h.center[1], "conf": h.conf}))
    return 0


def cmd_tree(args):
    drv = get_driver(_target(args))
    nodes = drv.ui_tree()
    if not nodes:
        print("(no accessibility tree for this target; use `th shot --ocr`)")
    for n in nodes:
        if args.filter and not n.matches(args.filter):
            continue
        if not args.all and not (n.label or n.identifier or n.value):
            continue
        print(f"{n.role:22} ({n.center[0]:5},{n.center[1]:5}) {n.w}x{n.h} {'' if n.enabled else '[disabled] '}"
              f"{n.label!r} {('id=' + n.identifier) if n.identifier else ''} {('val=' + n.value) if n.value else ''}")
    return 0


def cmd_wait_text(args):
    drv = get_driver(_target(args))
    ok = drv.wait_for_text(args.text, timeout=args.timeout)
    print("found" if ok else "not found")
    return 0 if ok else 1


def cmd_ocr(args):
    from .ocr import dump_text, find_text
    p = Path(args.image)
    if args.find:
        for h in find_text(p, args.find):
            print(json.dumps({"text": h.text, "cx": h.center[0], "cy": h.center[1], "conf": h.conf}))
    else:
        print(dump_text(p))
    return 0


# ---------------- runs / reports -----------------

def cmd_run(args):
    from .report import Run, CURRENT
    if args.sub == "new":
        from .build import BuildInfo
        bi = BuildInfo.load()
        meta = {"target": _target(args), "title": args.title or ""}
        if bi:
            meta["build"] = bi.__dict__
        r = Run.new(args.name, meta)
        print(r.path)
    elif args.sub == "current":
        print(Run.current().path)
    elif args.sub == "use":
        CURRENT.write_text(str(Path(args.path).resolve())); print(args.path)
    elif args.sub == "note":
        r = Run.current(args.run); r.log_step("note", args.text); print("ok")
    elif args.sub == "finish":
        r = Run.current(args.run); r.finish(args.status, args.summary or ""); print(r.path)
    elif args.sub == "list":
        for p in sorted(CONFIG.runs_dir.iterdir()):
            if p.is_dir():
                m = json.loads((p / "run.json").read_text()) if (p / "run.json").exists() else {}
                print(f"{p.name}  {m.get('status','?'):8} {m.get('target','?'):6} shots={len(list((p/'screenshots').glob('*.png')))}")
    elif args.sub == "steps":
        r = Run.current(args.run)
        for s in r.steps():
            print(f"{s['t'][11:19]} {s['action']:28} {s.get('note','')}  {s.get('screenshot','')}")
    return 0


def cmd_report(args):
    from .report import Run, render_html, post_to_pr
    r = Run.current(args.run)
    if args.sub == "html":
        print(render_html(r, Path(args.out) if args.out else None))
    elif args.sub == "pr":
        print(post_to_pr(r, args.pr, mode=args.mode, columns=args.columns, max_shots=args.max_shots))
    elif args.sub == "markdown":
        from .report import upload_screenshots, render_markdown
        urls = upload_screenshots(r) if args.upload else {s: s for s in [x.get("screenshot") for x in r.steps()] if s}
        print(render_markdown(r, urls, columns=args.columns))
    return 0


# ---------------- flows / tests -----------------

def cmd_flow(args):
    from .flows import trio as F
    from .report import Run
    drv = get_driver(_target(args))
    try:
        r = Run.current(args.run)
    except HarnessError:
        r = None
    if args.sub == "onboarding-walk":
        F.walk_onboarding(drv, r)
    elif args.sub == "add-cgm-sim":
        F.add_cgm_simulator(drv, r)
    elif args.sub == "add-pump-sim":
        F.add_pump_simulator(drv, r)
    elif args.sub == "back-home":
        F.back_to_home(drv)
    elif args.sub == "dismiss":
        print(F.dismiss_dialogs(drv))
    elif args.sub == "at-home":
        ok = F.at_home(drv); print("home" if ok else "not home"); return 0 if ok else 1
    return 0


def cmd_test(args):
    """Smoke test: launch Trio, get to the home screen, add fake CGM + pump, screenshot everything."""
    from .flows import trio as F
    from .report import Run, render_html
    from .build import BuildInfo
    target = _target(args)
    bi = BuildInfo.load()
    bundle = bi.bundle_id if bi else CONFIG.bundle_id(target)
    r = Run.new(args.name or f"smoke-{target}", {"target": target, "build": bi.__dict__ if bi else {},
                                                  "title": f"Trio smoke test ({target})"})
    drv = get_driver(target)
    try:
        if target == "sim":
            drv.boot()
            if not drv.is_installed(bundle):
                raise HarnessError("Trio is not installed on the simulator; run `th build && th install`")
            if args.preseed:
                F.preseed_onboarding(drv.app_data_container(bundle))
            drv.launch(bundle)
        else:
            drv.wake()
            drv.open_app_via_spotlight("Trio")
        time.sleep(4)
        F.step(r, drv, "launch", "Trio launched")
        F.dismiss_dialogs(drv, ["OK", "Continue", "Dismiss"])
        if not F.at_home(drv):
            F.walk_onboarding(drv, r)
        F.step(r, drv, "home", "Home screen reached")
        if args.devices:
            F.add_cgm_simulator(drv, r)
            F.back_to_home(drv)
            F.add_pump_simulator(drv, r)
            F.back_to_home(drv)
            time.sleep(3)
            F.step(r, drv, "home-with-devices", "Home with Glucose Simulator + Pump Simulator")
        r.finish("passed", "Trio launched and reached the home screen" + (" with fake CGM and pump configured." if args.devices else "."))
    except Exception as e:  # noqa: BLE001
        F.step(r, drv, "failure", str(e)[:200])
        r.finish("failed", f"{type(e).__name__}: {e}")
        render_html(r)
        raise
    print(render_html(r))
    print(r.path)
    return 0


# ---------------- argparse -----------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="th", description="Trio test harness")
    p.add_argument("--target", choices=["sim", "device"], help="default: $TH_TARGET or sim")
    sp = p.add_subparsers(dest="cmd", required=True)

    sp.add_parser("doctor", help="check the environment").set_defaults(fn=cmd_doctor)

    b = sp.add_parser("build", help="check out + build Trio")
    b.add_argument("--branch", help="branch | owner/repo@branch | URL#branch")
    b.add_argument("--pr", help="PR number | owner/repo#N | PR URL")
    b.add_argument("--repo", help="GitHub owner/repo to fetch --branch from (default: Trio origin)")
    b.add_argument("--in-place", help="build this existing checkout as-is (default when no --branch/--pr: $TRIO_REPO)")
    b.add_argument("--submodule", action="append", default=[], help="NAME@REF | NAME@pr/N | NAME=URL#REF (repeatable)")
    b.add_argument("--configuration", default="Debug")
    b.add_argument("--clean", action="store_true")
    b.add_argument("--trollstore", action="store_true", help="device: ad-hoc sign and package an .ipa for TrollStore")
    b.add_argument("--no-fetch", action="store_true", help="skip submodule sync")
    b.add_argument("--checkout-only", action="store_true")
    b.set_defaults(fn=cmd_build)

    i = sp.add_parser("install", help="install the last build")
    i.add_argument("--app"); i.add_argument("--ipa"); i.add_argument("--reinstall", action="store_true")
    i.add_argument("--trollstore", action="store_true"); i.add_argument("--open-on-device", action="store_true",
                                                                          help="type the TrollStore link into Safari via VNC")
    i.add_argument("--serve-seconds", type=int, default=600)
    i.set_defaults(fn=cmd_install)

    l = sp.add_parser("launch", help="launch Trio")
    l.add_argument("--bundle-id"); l.add_argument("--preseed-onboarding", action="store_true")
    l.add_argument("--via", choices=["vnc", "devicectl"], default="vnc"); l.add_argument("--app-name", default="Trio")
    l.set_defaults(fn=cmd_launch)

    s = sp.add_parser("sim", help="simulator management"); ss = s.add_subparsers(dest="sub", required=True)
    for name in ("list", "runtimes", "boot", "shutdown", "erase"):
        ss.add_parser(name)
    ir = ss.add_parser("install-runtime"); ir.add_argument("version", nargs="?")
    c = ss.add_parser("container"); c.add_argument("--bundle-id")
    d = ss.add_parser("defaults"); d.add_argument("key"); d.add_argument("value"); d.add_argument("--type", default="-string"); d.add_argument("--bundle-id")
    ou = ss.add_parser("openurl"); ou.add_argument("url")
    s.set_defaults(fn=cmd_sim)

    dv = sp.add_parser("device", help="physical iPhone utilities"); ds = dv.add_subparsers(dest="sub", required=True)
    ds.add_parser("list"); ds.add_parser("wake")
    vc = ds.add_parser("vnc-check"); vc.add_argument("--out")
    si = ds.add_parser("serve-ipa"); si.add_argument("ipa"); si.add_argument("--serve-seconds", type=int, default=600)
    dv.set_defaults(fn=cmd_device)

    sh = sp.add_parser("shot", help="screenshot into the current run"); sh.add_argument("--name"); sh.add_argument("--note")
    sh.add_argument("--out"); sh.add_argument("--run"); sh.add_argument("--ocr", action="store_true"); sh.set_defaults(fn=cmd_shot)

    t = sp.add_parser("tap"); t.add_argument("x", type=int); t.add_argument("y", type=int); t.add_argument("--duration", type=float, default=0); t.set_defaults(fn=cmd_tap)
    sw = sp.add_parser("swipe"); [sw.add_argument(n, type=int) for n in ("x1", "y1", "x2", "y2")]; sw.add_argument("--duration", type=float, default=0.3); sw.set_defaults(fn=cmd_swipe)
    sc = sp.add_parser("scroll"); sc.add_argument("direction", choices=["up", "down", "left", "right"]); sc.add_argument("--amount", type=float, default=0.5); sc.set_defaults(fn=cmd_scroll)
    ty = sp.add_parser("type"); ty.add_argument("text"); ty.set_defaults(fn=cmd_type)
    k = sp.add_parser("key"); k.add_argument("name"); k.set_defaults(fn=cmd_key)
    sp.add_parser("home").set_defaults(fn=cmd_home)
    tt = sp.add_parser("tap-text", help="tap element by label/id (accessibility tree, OCR fallback)")
    tt.add_argument("text"); tt.add_argument("--exact", action="store_true"); tt.add_argument("--index", type=int, default=0); tt.set_defaults(fn=cmd_tap_text)
    f = sp.add_parser("find"); f.add_argument("text"); f.add_argument("--exact", action="store_true"); f.set_defaults(fn=cmd_find)
    tr = sp.add_parser("tree", help="dump accessibility tree (simulator)"); tr.add_argument("--filter"); tr.add_argument("--all", action="store_true"); tr.set_defaults(fn=cmd_tree)
    wt = sp.add_parser("wait-text"); wt.add_argument("text"); wt.add_argument("--timeout", type=float, default=20); wt.set_defaults(fn=cmd_wait_text)
    oc = sp.add_parser("ocr", help="OCR an image file"); oc.add_argument("image"); oc.add_argument("--find"); oc.set_defaults(fn=cmd_ocr)

    r = sp.add_parser("run", help="test-run bookkeeping"); rs = r.add_subparsers(dest="sub", required=True)
    rn = rs.add_parser("new"); rn.add_argument("name"); rn.add_argument("--title")
    rs.add_parser("current"); rs.add_parser("list")
    ru = rs.add_parser("use"); ru.add_argument("path")
    rno = rs.add_parser("note"); rno.add_argument("text"); rno.add_argument("--run")
    rf = rs.add_parser("finish"); rf.add_argument("status", choices=["passed", "failed", "partial"]); rf.add_argument("--summary"); rf.add_argument("--run")
    rst = rs.add_parser("steps"); rst.add_argument("--run")
    r.set_defaults(fn=cmd_run)

    rp = sp.add_parser("report", help="render/post reports"); rps = rp.add_subparsers(dest="sub", required=True)
    rh = rps.add_parser("html"); rh.add_argument("--run"); rh.add_argument("--out")
    rpr = rps.add_parser("pr"); rpr.add_argument("pr"); rpr.add_argument("--run"); rpr.add_argument("--mode", choices=["comment", "description"], default="comment")
    rpr.add_argument("--columns", type=int, default=3); rpr.add_argument("--max-shots", type=int)
    rm = rps.add_parser("markdown"); rm.add_argument("--run"); rm.add_argument("--upload", action="store_true"); rm.add_argument("--columns", type=int, default=3)
    rp.set_defaults(fn=cmd_report)

    fl = sp.add_parser("flow", help="Trio UI flows"); fls = fl.add_subparsers(dest="sub", required=True)
    for name in ("onboarding-walk", "add-cgm-sim", "add-pump-sim", "back-home", "dismiss", "at-home"):
        x = fls.add_parser(name); x.add_argument("--run")
    fl.set_defaults(fn=cmd_flow)

    te = sp.add_parser("test", help="end-to-end smoke test on the installed build")
    te.add_argument("kind", choices=["smoke"]); te.add_argument("--name"); te.add_argument("--no-preseed", dest="preseed", action="store_false")
    te.add_argument("--no-devices", dest="devices", action="store_false"); te.set_defaults(fn=cmd_test)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except HarnessError as e:
        print(f"th: error: {e}", file=sys.stderr)
        return 2
    except LookupError as e:
        print(f"th: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
