"""`th` command line: one entry point for everything the Claude skills call. `--app loop` (or TH_APP=loop) points every
command at Loop instead of Trio (harness/apps.py)."""
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


TARGET_ALIASES = {"sim": "sim", "simulator": "sim",
                  "device": "rooted-vnc", "rooted-vnc": "rooted-vnc", "vnc": "rooted-vnc", "phone": "rooted-vnc",
                  "iphone-usb": "iphone-usb", "mirror": "iphone-usb", "mirroring": "iphone-usb",
                  "watch": "watch", "watch-sim": "watch"}
TARGETS = ("sim", "rooted-vnc", "iphone-usb", "watch")


def norm_target(t: str | None) -> str:
    t = (t or os.environ.get("TH_TARGET") or "sim").lower()
    if t not in TARGET_ALIASES:
        raise HarnessError(f"unknown target {t!r} (sim | rooted-vnc | iphone-usb | watch)")
    return TARGET_ALIASES[t]


def get_driver(target: str, *, local: bool = False):
    """Local driver, or a proxy to `th daemon` when one is running for this target.

    sim        — iOS Simulator (simctl + WebDriverAgent)
    rooted-vnc — jailbroken iPhone over TrollVNC (+ SSH), screenshots/taps at native resolution
    iphone-usb — any iPhone through macOS iPhone Mirroring (phone-harness), no root
    watch      — the Watch app on a watchOS simulator paired with the sim (harness/watch.py)
    """
    target = norm_target(target)
    if not local and not os.environ.get("TH_NO_DAEMON"):
        from . import daemon as D
        if D.is_running(target):
            return D.RemoteDriver(target)
    if target == "sim":
        from .sim import SimulatorDriver
        return SimulatorDriver()
    if target == "watch":
        from .watch import WatchDriver
        return WatchDriver()
    if target == "rooted-vnc":
        from .vnc import VNCDriver
        return VNCDriver()
    from .mirror import MirrorDriver
    return MirrorDriver()


def _target(args) -> str:
    return norm_target(getattr(args, "target", None))


def _run(args):
    from .report import Run
    return Run.current(getattr(args, "run", None))


def _app():
    from . import apps
    return apps.current()


# ---------------- doctor -----------------

def cmd_doctor(args):
    """Environment check, per connection mode. `th doctor` = general + all modes; `th --target X doctor` = that mode only."""
    from . import xcode
    ok = True
    want = norm_target(getattr(args, "target", None)) if getattr(args, "target", None) else None

    def item(name, good, detail="", *, warn=False):
        nonlocal ok
        if not good and not warn:
            ok = False
        tag = "ok" if good else ("warn" if warn else "MISSING")
        print(f"  [{tag}] {name}{': ' + detail if detail else ''}")

    print("General")
    app = xcode.find_xcode()
    item("Xcode.app", app, f"{app} (v{xcode.xcode_version(app)})" if app else "run ./bootstrap-xcode.sh")
    for t in ("gh", "git", "tesseract"):
        item(t, which(t), which(t) or "")
    cp = run(["gh", "auth", "status"], check=False, quiet=True); item("gh auth", cp.returncode == 0)
    from . import build as B
    try:
        proto = B.git_protocol()
        # TandemKit is private, so listing it checks the credentials as well as the transport
        url = "https://github.com/jwoglom/TandemKit.git"
        cp = run(["git", "ls-remote", url, "HEAD"], check=False, quiet=True, env=B.git_env(), timeout=30)
        hint = {"ssh": "no usable SSH key: ssh-add it, or TH_GIT_PROTOCOL=https", "https": "gh auth login, or TH_GIT_PROTOCOL=ssh",
                "token": "TH_GIT_TOKEN cannot read it"}[proto]
        item(f"GitHub git access ({proto})", cp.returncode == 0, "private TandemKit readable" if cp.returncode == 0 else hint)
    except HarnessError as e:
        item("GitHub git access", False, str(e))
    try:
        import pymobiledevice3, requests, PIL  # noqa: F401
        item("python deps", True)
    except ImportError as e:
        item("python deps", False, str(e))
    a = _app()
    item(f"{a.repo_key} checkout ({a.name})", True, str(a.checkout)
         + ("" if (a.checkout / a.workspace).exists() else " (cloned on first build)"))
    item("harness.env", ENV_FILE.exists(), str(ENV_FILE) if ENV_FILE.exists() else "cp harness.env.example harness.env")
    free = shutil.disk_usage("/").free // 2**30
    item("disk free", free >= 12, f"{free} GB (a {a.name} build needs ~10 GB of derived data)", warn=free >= 8)
    from . import daemon as DM

    if want in (None, "sim"):
        print("simulator")
        if app:
            try:
                rts = xcode.list_runtimes(); item("iOS runtime", rts, ", ".join(r["name"] for r in rts) or "th sim install-runtime")
                wrts = xcode.list_runtimes("watchOS"); item(f"watchOS runtime ({a.name} embeds a Watch app)", wrts,
                                                            ", ".join(r["name"] for r in wrts) or "th sim install-runtime --platform watchOS")
                devs = [d for d in xcode.list_devices() if d["name"] == CONFIG.get("TH_SIM_NAME")]
                item(f"simulator '{CONFIG.get('TH_SIM_NAME')}' ({CONFIG.get('TH_SIM_DEVICE')})", devs,
                     f"{devs[0]['state']} {devs[0]['runtimeIdentifier'].split('.')[-1]}" if devs else "created by th sim boot", warn=not devs)
            except HarnessError as e:
                item("simctl", False, str(e)[:160])
        from . import wda as W
        xr = W.xctestrun(); item("WebDriverAgent built", xr, xr.name if xr else "th wda build")
        item("WebDriverAgent running", W.is_running(), "port 8100" if W.is_running() else "th wda start (after th sim boot)", warn=True)
        item("sim daemon", DM.is_running("sim"), "" if DM.is_running("sim") else "th --target sim daemon start", warn=True)
        item("idb (optional fallback)", which("idb_companion"), which("idb_companion") or "not needed with WDA", warn=True)

    if want in (None, "sim", "watch"):
        print("watch (paired with the simulator; --watch / --target watch)")
        from . import watch as WT
        try:
            cur = WT.current_pair()
            item("phone + watch pair in use", cur, f"{WT.model_name(cur[0])}" if cur else
                 "none yet: th watch pair (each watch model gets its own phone simulator)", warn=True)
        except HarnessError as e:
            item("simctl pairs", False, str(e)[:160], warn=True)
        xr = WT.xctestrun(); item("watch agent built", xr, xr.name if xr else "th watch agent build (or start)", warn=True)
        item("watch agent running", WT.is_running(), "" if WT.is_running() else "th watch agent start", warn=True)

    if want in (None, "rooted-vnc"):
        print("rooted-vnc (jailbroken iPhone)")
        from . import devices as D
        try:
            devs = D.usbmux_devices()
            item("usbmuxd device", devs, "; ".join(f"{d.get('name','?')} iOS {d.get('ios','?')} ({d['connection']})" for d in devs) or "plug in + trust")
        except HarnessError as e:
            item("usbmuxd", False, str(e)[:120])
        item("TH_DEVICE_UDID", CONFIG.get("TH_DEVICE_UDID"), "set" if CONFIG.get("TH_DEVICE_UDID") else "blank = only connected device", warn=True)
        item("iproxy", which("iproxy"), which("iproxy") or "brew install libusbmuxd")
        item("TH_VNC_PASSWORD", CONFIG.get("TH_VNC_PASSWORD"), "set" if CONFIG.get("TH_VNC_PASSWORD") else "TrollVNC full-access password, in .env")
        item("TH_DEVICE_PASSCODE", CONFIG.get("TH_DEVICE_PASSCODE"), "set" if CONFIG.get("TH_DEVICE_PASSCODE") else "needed for th device unlock, in .env")
        try:
            from .vnc import VNCDriver
            v = VNCDriver(); w, h = v.screen_size(); v.close()
            item("TrollVNC connect + auth", True, f"{w}x{h} via {'USB iproxy' if v.via_usb else v.host}")
        except Exception as e:  # noqa: BLE001
            item("TrollVNC connect + auth", False, str(e).splitlines()[0][:140])
        ssh = D.ssh_ok(); item("SSH (mobile@phone, key auth)", ssh, " ".join(D.ssh_target()[1:2]) if ssh else "ssh mobile@<phone> must work without a password")
        if ssh:
            cp = D.ssh_run("ls /var/jb/Library/MobileSubstrate/DynamicLibraries/ 2>/dev/null | grep -c AppSyncUnified; command -v uiopen", check=False, timeout=30)
            item("AppSync Unified on phone", cp.stdout.strip().startswith(("1", "2", "3", "4")), "installd hook present" if cp.returncode == 0 else "see docs/phone-setup.md")
            item("uiopen on phone", "uiopen" in cp.stdout, "")
        item("rooted-vnc daemon", DM.is_running("rooted-vnc"), "" if DM.is_running("rooted-vnc") else "th --target rooted-vnc daemon start", warn=True)

    if want in (None, "iphone-usb"):
        print("iphone-usb (iPhone Mirroring)")
        try:
            from .mirror import MirrorDriver
            st = MirrorDriver().status()
            item("iPhone Mirroring app running", st["app_running"], "", warn=True)
            item("mirroring window", bool(st["window"]), str(st["window"]) if st["window"] else "pair iPhone Mirroring first", warn=True)
        except HarnessError as e:
            item("phone-harness", False, str(e)[:120], warn=True)
        item("TH_DEVELOPER_TEAM / your own override's team (signed builds)", CONFIG.get("TH_DEVELOPER_TEAM") or a.user_team(a.checkout),
             "set" if CONFIG.get("TH_DEVELOPER_TEAM") else "needed only for Xcode installs (th build/install --xcode, iphone-usb)", warn=True)

    print("\nsummary:", "ready" if ok else "some required items missing (see MISSING above)")
    return 0 if ok else 1


# ---------------- build / install / launch -----------------

def cmd_build(args):
    from . import build as B
    src, info = B.prepare_source(branch=args.branch, pr=args.pr, repo=args.repo,
                                 in_place=Path(args.in_place) if args.in_place else None,
                                 submodules=args.submodule, fetch=not args.no_fetch, trio_branch=args.trio_branch,
                                 cherry_picks=args.cherry_pick, submodule_cherry_picks=args.submodule_cherry_pick)
    if args.checkout_only:
        print(json.dumps(info, indent=2)); return 0
    target = _target(args)
    if args.xcode and args.trollstore:
        raise HarnessError("--xcode (team-signed) and --trollstore (fake-signed) are different builds; pick one")
    # build() knows sim | device; rooted-vnc / iphone-usb are device builds (the pipeline does the same mapping, and
    # without it the --trollstore fake-sign + .ipa step was silently skipped for `th --target rooted-vnc build`).
    # --xcode is always a team-signed device build, whatever the target.
    build_target = "sim" if target == "sim" and not args.xcode else "device"
    bi = B.build(src, build_target, configuration=args.configuration, clean=args.clean, git_info=info,
                 trollstore=not args.xcode and (args.trollstore or target == "rooted-vnc"))
    print(json.dumps(bi.__dict__, indent=2))
    return 0


def cmd_install(args):
    from .build import BuildInfo
    bi = BuildInfo.load()
    app = Path(args.app_path) if args.app_path else (Path(bi.app_path) if bi else None)
    if not app or not app.exists():
        raise HarnessError("no built app; run `th build` first or pass --app-path")
    target = _target(args)
    from . import devices as D
    if args.xcode:
        if bi and not args.app_path and bi.target != "device":
            raise HarnessError(f"the last build is a {bi.target} build; Xcode installs need a team-signed device "
                               "build: `th build --xcode`")
        dev = D.xcode_install(app, args.device or (CONFIG.get("TH_DEVICE_UDID") if target == "rooted-vnc" else None))
        log(f"installed {app.name} on {dev} via Xcode (devicectl)")
        return 0
    if target == "sim":
        drv = get_driver("sim"); drv.boot()
        if args.reinstall and bi:
            drv.uninstall(bi.bundle_id)
        drv.install(app)
        log(f"installed {app.name} on simulator {drv.udid}")
        return 0
    if target == "watch":
        drv = get_driver("watch"); drv.boot()
        if args.reinstall and bi:
            drv.uninstall(bi.bundle_id)
        drv.install(app)
        log(f"installed {app.name}'s Watch app on watch simulator {drv.udid}")
        return 0
    # physical phones
    if target == "iphone-usb" and not (args.appsync or args.trollstore):
        dev = D.xcode_install(app, args.device)      # needs a signed build (TH_DEVELOPER_TEAM) and Developer Mode
        log(f"installed on device {dev} via Xcode (devicectl)")
        return 0
    if args.appsync:
        ipa = Path(args.ipa) if args.ipa else (Path(bi.ipa_path) if bi and bi.ipa_path else None)
        if not ipa or not ipa.exists():
            from .build import package_ipa
            ipa = package_ipa(app, CONFIG.build_dir / f"{_app().name}.ipa")
        D.appsync_install(ipa, CONFIG.get("TH_DEVICE_UDID") or None)
        log(f"installed {ipa.name} via installd (AppSync Unified)")
        return 0
    if args.trollstore or (bi and bi.target == "device-trollstore"):
        ipa = Path(args.ipa) if args.ipa else (Path(bi.ipa_path) if bi and bi.ipa_path else None)
        if not ipa or not ipa.exists():
            from .build import package_ipa
            ipa = package_ipa(app, CONFIG.build_dir / f"{_app().name}.ipa")
        url, httpd = D.trollstore_install_url(ipa)
        print(f"\nTrollStore install link (open on the phone):\n  {url}\n")
        if args.open_on_device:
            drv = get_driver("rooted-vnc")
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
    dev = D.xcode_install(app, args.device or CONFIG.get("TH_DEVICE_UDID") or None)
    log(f"installed on device {dev} via Xcode (devicectl)")
    return 0


def cmd_launch(args):
    from .build import BuildInfo
    bi = BuildInfo.load()
    bundle = args.bundle_id or (bi.bundle_id if bi else CONFIG.bundle_id(_target(args)))
    target = _target(args)
    F = _app().flows
    app_name = args.app_name or _app().name
    if target == "sim":
        drv = get_driver("sim"); drv.boot()
        if args.preseed_onboarding:
            if not drv.is_installed(bundle):
                raise HarnessError(f"{bundle} not installed on the simulator; run `th install` first")
            paths = F.preseed_simulator(drv, bundle)
            log(f"pre-seeded onboarding: {[str(p) for p in paths]}")
        drv.launch(bundle)
        return 0
    if target == "watch":
        drv = get_driver("watch"); drv.boot()
        drv.launch(bundle)
        log("Watch app launched; launch the phone app after it (`th launch`), or its WCSession may not see the watch")
        return 0
    from . import devices as D
    if target == "iphone-usb":
        drv = get_driver("iphone-usb"); drv.boot()
        drv.open_app_via_spotlight(app_name)
        return 0
    if args.preseed_onboarding and D.ssh_ok():
        if F.preseed_phone(bundle):
            log("pre-seeded onboarding/diagnostics flags over SSH")
        else:
            log(f"{_app().name} cannot be pre-seeded on a phone; walk it with `th flow onboarding-walk`")
    if args.via == "devicectl":
        D.devicectl_launch(D.pick_udid(), bundle)
    elif args.via == "ssh" or (args.via == "auto" and D.ssh_ok()):
        D.ssh_launch(bundle); log(f"launched {bundle} via uiopen over SSH")
    else:
        drv = get_driver("device"); drv.wake(); drv.open_app_via_spotlight(app_name)
    return 0


def cmd_faketandem(args):
    from . import faketandem as FT
    if args.sub == "start":
        state = FT.start(setup=args.setup, scenario=args.scenario, ref=args.ref)
        if args.seed:
            _seed_faketandem_pump(state)
        print(json.dumps({k: v for k, v in state.items() if k != "long_term_key"}, indent=2))
        for line in FT.summary():
            print(line)
        return 0
    if args.sub == "stop":
        save = Path(_run(args).path) if args.save else None
        print("stopped" if FT.stop(save_to=save) else "not running")
        return 0
    if args.sub == "seed":
        state = FT.current()
        if not state:
            raise HarnessError("no faketandem is running; `th faketandem start --setup preseed` first")
        _seed_faketandem_pump(state)
        return 0
    state = FT.current()
    if not state:
        raise HarnessError("no faketandem is running (`th faketandem start`, or trio-test-e2e --pump faketandem)")
    if args.sub == "status":
        snapshot = FT.api("GET", "/api/state")
        if args.json:
            print(json.dumps(snapshot, indent=2))
            return 0
        print(f"faketandem {state.get('sha') or state.get('source')}: {state['setup']}"
              + (f", scenario {state['scenario']}" if state.get("scenario") else "")
              + f"; GATT {state['gatt']}, API {state['api']}, log {state['log']}")
        for line in FT.summary(snapshot):
            print(line)
        return 0
    if args.sub == "pairing":
        print(FT.set_pairing_state(args.state) if args.state else FT.api("GET", "/api/bluetooth/pairingstate")["pairingState"])
        return 0
    if args.sub == "history":
        if args.all:
            print(json.dumps(FT.api("GET", f"/api/history?limit={args.limit}"), indent=2))
        else:
            print("\n".join(FT.history_doses(args.limit)))
        return 0
    if args.sub == "scenario":
        body = json.loads(Path(args.scenario).read_text()) if Path(args.scenario).is_file() else {"preset": args.scenario}
        result = FT.api("POST", "/api/state/scenario", body)
        print(json.dumps(result.get("scenario"), indent=2))
        return 0
    if args.sub == "api":
        body = json.loads(args.body) if args.body else None
        print(json.dumps(FT.api(args.method, args.path, body), indent=2))
        return 0
    raise HarnessError(f"unknown faketandem command {args.sub}")


def cmd_faceid(args):
    if _target(args) != "sim":
        raise HarnessError("faceid is for the simulator (--target sim)")
    get_driver("sim").face_id(args.action)
    print(args.action)
    return 0


def _seed_faketandem_pump(state: dict) -> None:
    """The installed app (simulator) with the pump already paired with this faketandem; `th launch` afterwards."""
    from . import faketandem as FT
    from .build import BuildInfo
    bi = BuildInfo.load()
    if not bi:
        raise HarnessError("no build to seed; `th build` first")
    drv = get_driver("sim")
    drv.terminate(bi.bundle_id)
    paths = _app().flows.preseed_virtual_pump(drv, bi.bundle_id, FT.paired_pump_manager_state(state), onboarding=False)
    log(f"seeded {[str(p) for p in paths]}; launch with `th launch`")


# ---------------- sim / device utilities -----------------

def cmd_busy(args):
    """Who is driving each target, from the device lock (harness/busy.py). Exit 1 if the asked target is busy."""
    from . import busy
    one = getattr(args, "target", None) or os.environ.get("TH_TARGET")
    targets = [norm_target(one)] if one else list(busy.TARGETS)
    held = False
    for t in targets:
        h = busy.holder(t)
        held = held or bool(h)
        print(f"{t:11} " + (f"busy: {busy.describe(h)}" if h else "idle"))
    return 1 if held and one else 0


def cmd_docs(args):
    """Search / browse the app's docs (https://triodocs.org, https://loopkit.github.io/loopdocs) as text
    (harness/triodocs.py)."""
    from . import triodocs as T
    if args.sub == "search":
        hits = T.search(" ".join(args.query), limit=args.limit)
        if not hits:
            print("no matches; try other words, or `th docs toc`")
        for h in hits:
            print(f"{h['title']}  —  {h['page']}\n  {h['url']}\n  {h['snippet']}\n")
    elif args.sub == "toc":
        for depth, title, path in T.toc():
            if args.depth and depth > args.depth:
                continue
            print(f"{'  ' * max(depth - 1, 0)}- {title}  ({path or '/'})")
    else:
        print(T.page(args.ref))
    return 0


def _docs_doc() -> str:
    from . import triodocs
    return triodocs.__doc__


def _busy_doc() -> str:
    from . import busy
    return busy.__doc__


def _applogs_doc() -> str:
    from . import applogs
    return applogs.__doc__


def cmd_logs(args):
    """Trio / TandemKit / watch log files from the installed app (harness/applogs.py)."""
    from . import applogs as A
    from .build import BuildInfo
    target = _target(args)
    bi = BuildInfo.load()
    built_for = "sim" if bi and bi.target == "sim" else "rooted-vnc" if bi and str(bi.target).startswith("device") else None
    bundle = args.bundle_id or (bi.bundle_id if bi and built_for == target else CONFIG.bundle_id(target))
    app_files = A.files_for(_app().key)
    if args.which not in ("all", *app_files):
        raise HarnessError(f"{_app().name} writes no {args.which} log file; it has: {', '.join(app_files)}")
    names = list(app_files) if args.which == "all" else [args.which]
    rels = [r for n in names for r in app_files[n]]
    got = A.fetch(target, bundle, rels)
    texts = {n: {r: got.get(r) for r in app_files[n]} for n in names}
    if args.files:
        for n, files in texts.items():
            for r, t in files.items():
                print(f"{n:7} Documents/{r:30} " + ("missing" if t is None else f"{len(t.splitlines())} lines, {len(t) // 1024} KB"))
        return 0
    if args.save:
        dest = _run(args).path / "logs"
        for p in A.save(texts, dest):
            print(p)
        return 0
    if all(t is None for f in texts.values() for t in f.values()):
        raise HarnessError(f"no log files yet in {bundle}'s Documents ({', '.join(rels)})")
    rows = A.query(texts, grep=args.grep, ignore_case=args.ignore_case,
                   since=A.parse_since(args.since) if args.since else None, level=args.level,
                   lines=args.lines or None)
    print("\n".join(rows) if rows else "(no matching entries)")
    return 0


def _appdebug_doc():
    from . import appdebug
    return appdebug.__doc__


def _debug_bundle(args, target: str) -> str:
    """--bundle-id, else the app as installed on the simulator / the last build's for the phone."""
    from . import appdb, applogs
    if args.bundle_id:
        return args.bundle_id
    return appdb.installed_bundle(get_driver("sim"), _app()) if target == "sim" else applogs.default_bundle(target)


def cmd_oslog(args):
    """The system log of the app's process (harness/appdebug.py)."""
    import re
    from . import appdebug as A, applogs
    target, app = _target(args), _app()
    bundle = _debug_bundle(args, target)
    if args.enable_debug or args.disable_debug:
        subs = [args.subsystem] if args.subsystem else [s.format(bundle=bundle) for s in A.DEBUG_SUBSYSTEMS[app.key]]
        mode = A.call(target, "oslog_config", subs, bool(args.enable_debug))
        print(f"{', '.join(subs)}: {mode}")
        return 0
    if args.stream:
        log(f"recording {app.name}'s log for {args.stream:g} s")
        entries = A.call(target, "oslog_stream", app.name, args.stream, args.level or "debug", args.subsystem, args.all)
    else:
        start = applogs.parse_since(args.since).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        entries = A.call(target, "oslog_entries", app.name, start, args.level or "info", args.subsystem, args.all)
    if args.grep:
        rx = re.compile(args.grep, re.I if args.ignore_case else 0)
        entries = [e for e in entries if rx.search(e["msg"]) or rx.search(e["category"])]
    lines = [A.format_entry(e) for e in entries]
    if args.save:
        dest = _run(args).path / "logs" / f"oslog-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n".join(lines) + "\n")
        print(f"{dest} ({len(lines)} entries)")
        return 0
    print("\n".join(lines[-args.lines:] if args.lines else lines) or "(no matching entries)")
    return 0


def cmd_crashes(args):
    """The app's crash reports (harness/appdebug.py)."""
    from . import appdebug as A, applogs
    target, app = _target(args), _app()
    bundle = _debug_bundle(args, target)
    run_ = None
    try:
        run_ = _run(args)
    except HarnessError:
        pass
    since = (applogs.parse_since(args.since).timestamp() if args.since
             else (A.run_started(run_) if run_ else None) or time.time() - 86400)
    if args.save:
        if not run_:
            raise HarnessError("--save needs a run (`th run new <name>` or --run DIR)")
        new = A.save_crashes(run_, target, app.name, bundle, since)
        for r in new:
            print(f"== crashes/{r['name']}\n{A.summarize_crash(r['text'], args.frames)}")
        print(f"{len(new)} new crash reports saved into {run_.path / 'crashes'}")
        return 0
    reports = A.call(target, "crash_reports", app.name, bundle, since)
    for r in reports:
        print(f"== {r['name']}\n{A.summarize_crash(r['text'], args.frames)}")
    if not reports:
        print(f"(no crash reports of {bundle} since {time.strftime('%Y-%m-%d %H:%M', time.localtime(since))})")
    return 0


def cmd_files(args):
    """Files in the app's Documents/ (harness/appdebug.py)."""
    from . import appdebug as A
    target = _target(args)
    bundle = _debug_bundle(args, target)
    if args.sub == "ls":
        files = A.call(target, "list_files", bundle, args.path or "")
        for f in files:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(f["mtime"])) if f.get("mtime") else ""
            print(f"{f['size']:>10}  {when:16}  {f['path']}")
        if not files:
            print(f"(no files under Documents/{args.path or ''})")
        return 0
    text = A.decode_file(args.path, A.call(target, "read_file", bundle, args.path))
    if args.save:
        dest = _run(args).path / "files" / args.path.lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        print(dest)
        return 0
    print(text)
    return 0


def cmd_perf(args):
    """CPU and memory of the running app (harness/appdebug.py)."""
    import csv
    from . import appdebug as A
    target, app = _target(args), _app()
    bundle = _debug_bundle(args, target)
    run_ = None
    if args.save:
        run_ = _run(args)
    if args.leaks:
        log("running leaks on the app (minutes on a small Mac)")
        r = A.call(target, "leaks", bundle)
        print(r["summary"])
        if r.get("restricted"):
            print("note: the build is not debuggable (no get-task-allow), so leaks saw only read-only memory: the "
                  "count is a lower bound")
        for count, size, name in r["types"]:
            print(f"{count:>6} {size:>10} B  {name}")
        if run_:
            run_.log_step("perf", r["summary"])
        return 0
    fields = ["time", "pid", "cpu", "rss_mb", "footprint_mb", "threads", "cpu_time"]
    rows = []
    start = time.time()
    for k in range(int((args.watch or 0) // args.interval) + 1):       # samples at 0, interval, ... watch seconds
        time.sleep(max(0.0, start + k * args.interval - time.time()))
        s = A.call(target, "perf_sample", app.name, bundle)
        s["time"] = time.strftime("%H:%M:%S", time.localtime(s.pop("t")))
        rows.append(s)
        print("  ".join(f"{k}={s.get(k)}" for k in fields))
    if run_:
        out = run_.path / "perf.csv"
        new = not out.exists()
        with open(out, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerows(rows)
        fp = [r["footprint_mb"] for r in rows if r.get("footprint_mb") is not None]
        note = (f"{len(rows)} samples: cpu avg {sum(r['cpu'] for r in rows) / len(rows):.1f}% max "
                f"{max(r['cpu'] for r in rows):.1f}%" + (f", footprint {min(fp)}-{max(fp)} MB" if fp else ""))
        run_.log_step("perf", note, file="perf.csv")
        print(f"{out}: {note}")
    return 0


def _appdb_doc():
    from . import appdb
    return appdb.__doc__


def cmd_db(args):
    """The app's Core Data store on the simulator (harness/appdb.py)."""
    from . import appdb as A
    if _target(args) != "sim":
        raise HarnessError("th db works on the simulator only (--target sim)")
    drv = get_driver("sim")
    bundle = args.bundle_id or A.installed_bundle(drv, _app())
    roots = A.containers(drv, bundle)
    if args.sub == "stores":
        default = A.STORES[_app().key]
        found = A.stores(roots)
        for p in found:
            print(("* " if p.name == default else "  ") + str(p))
        if not found:
            print(f"(no SQLite files readable in {bundle}'s containers)")
        return 0
    store = A.pick_store(roots, _app().key, args.store)
    if args.sub == "tables":
        for name, n in A.tables(store):
            print(f"{name:40} {n}")
    elif args.sub == "schema":
        for name, kind in A.schema(store, args.table):
            print(f"{name:40} {kind}")
    elif args.sub == "query":
        cols, rows = A.query(store, args.sql)
        if args.json:
            print(json.dumps([dict(zip(cols, r)) for r in rows], indent=1, default=str))
        else:
            print("\t".join(cols))
            for r in rows:
                print("\t".join("NULL" if v is None else str(v) for v in r))
        print(f"({len(rows)} rows)", file=sys.stderr)
    elif args.sub == "exec":
        run_ = None
        try:
            run_ = _run(args)
        except HarnessError:
            pass
        A.wait_closed(drv, bundle, store)
        saved = A.backup(store, run_.path / "db") if run_ else None
        if saved:
            log(f"saved {store.name} as it was before this run's first change: {saved}")
        try:
            n = A.execute(store, args.sql)
            print(f"{n} rows changed in {store.name}")
            if run_:
                run_.log_step("db", f"{n} rows changed in {store.name}: {args.sql}")
        finally:                    # a failed change leaves the store as it was; bring the app back either way
            if args.relaunch:
                drv.launch(bundle)
                log(f"relaunched {bundle}")
    return 0


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
        cmd = ["xcodebuild", "-downloadPlatform", args.platform]
        if args.version:
            cmd += ["-buildVersion", args.version]
        run(cmd, env=xcode.developer_env(), capture=False, timeout=7200)
    elif sub == "boot":
        drv = get_driver("sim"); drv.boot(); print(drv.udid)
    elif sub == "shutdown":
        d = xcode.ensure_simulator(); xcode.xcrun(["simctl", "shutdown", d["udid"]], check=False)
    elif sub == "erase":
        d = xcode.ensure_simulator()
        if any(p["phone"]["udid"] == d["udid"] for p in xcode.list_pairs().values()):
            raise HarnessError("this phone is paired with a watch, and an erased phone no longer links Watch apps: "
                               "`th watch pair --recreate` makes the pair anew")
        xcode.xcrun(["simctl", "shutdown", d["udid"]], check=False)
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
            for d in D.xcode_devices():
                print("devicectl:", json.dumps(d))
    elif args.sub == "vnc-check":
        drv = get_driver("rooted-vnc")
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
    elif args.sub == "mirror-status":
        from .mirror import MirrorDriver
        print(json.dumps(MirrorDriver().status(), default=str))
    elif args.sub == "wake":
        get_driver("rooted-vnc").wake()
    elif args.sub == "lock":
        get_driver("rooted-vnc").power(); print("locked (power pressed); `th device unlock` to come back")
    elif args.sub == "unlock":
        ok = get_driver("rooted-vnc").unlock(); print("unlocked" if ok else "still locked"); return 0 if ok else 1
    elif args.sub == "ssh":
        cp = D.ssh_run(" ".join(args.cmd), check=False, timeout=args.timeout)
        sys.stdout.write(cp.stdout); sys.stderr.write(cp.stderr); return cp.returncode
    elif args.sub == "diag":
        print(D.diag())
    elif args.sub == "vnc-restart":
        print(D.vnc_restart())
        from . import daemon as DM
        if DM.is_running("device"):
            DM.stop("device"); time.sleep(1); DM.start("device")
            print("device daemon restarted")
    elif args.sub == "kill":
        D.ssh_kill(args.name); print("ok")
    elif args.sub == "apps":
        print(D.ssh_installed_apps())
    return 0


def cmd_daemon(args):
    from . import daemon as D
    t = _target(args)
    if args.sub == "start":
        D.start(t, foreground=args.foreground); print(f"daemon[{t}] running" if not args.foreground else "")
    elif args.sub == "stop":
        D.stop(t); print(f"daemon[{t}] stopped")
    elif args.sub == "status":
        for tt in TARGETS:
            print(f"{tt}: {'running' if D.is_running(tt) else 'not running'}")
    elif args.sub == "restart":
        D.stop(t); time.sleep(0.5); D.start(t); print(f"daemon[{t}] restarted")
    return 0


def cmd_wda(args):
    from . import wda as W
    drv = get_driver("sim")
    if args.sub == "build":
        print(W.build(drv.udid))
    elif args.sub == "start":
        drv.boot(); W.start(drv.udid); print(f"WDA ready on :{W.PORT}")
    elif args.sub == "stop":
        W.stop(); print("stopped")
    elif args.sub == "status":
        print("running" if W.is_running() else "not running"); return 0 if W.is_running() else 1
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


def cmd_drag(args):
    get_driver(_target(args)).drag(args.x1, args.y1, args.x2, args.y2, hold=args.hold, duration=args.duration, settle=args.settle); return 0


def cmd_scroll(args):
    get_driver(_target(args)).scroll(args.direction, args.amount); return 0


def cmd_type(args):
    get_driver(_target(args)).type_text(args.text); return 0


def cmd_key(args):
    get_driver(_target(args)).key(args.name); return 0


def cmd_home(args):
    get_driver(_target(args)).home(); return 0


def cmd_crown(args):
    target = _target(args)
    if target != "watch":
        print("the Digital Crown is on the watch: `th --target watch crown 0.5`", file=sys.stderr); return 2
    get_driver(target).crown(args.delta, velocity=args.velocity); return 0


def cmd_watch(args):
    from . import watch as W
    from . import xcode
    if args.sub == "pair":
        from .build import BuildInfo
        bi = BuildInfo.load()
        cur = W.current_pair()
        model = args.model or (W.model_name(cur[0]) if cur else None)
        print(json.dumps(get_driver("watch").use_model(
            model, app_path=bi.app_path if bi else None, bundle_id=bi.bundle_id if bi else None,
            wrist=args.wrist, crown=args.crown, text_size=args.text_size, recreate=args.recreate), indent=1))
        print("Every command now drives this pair's phone. A new pair's phone has no fake CGM or pump yet "
              "(th flow add-cgm-sim, th flow add-pump-sim).")
    elif args.sub == "status":
        cur = W.current_pair()
        if not cur:
            print("pair: none in use (th watch pair [--model ...])")
        else:
            st = next(p["state"] for p in xcode.list_pairs().values() if p["watch"]["udid"] == cur[0])
            print(f"pair: {W.model_name(cur[0])}, watch {cur[0]}, phone {cur[1]} {st}")
        print(f"agent: {'running' if W.is_running() else 'not running (th watch agent start)'}")
        if W.is_running():
            print(f"front app: {W.call('state')}")
    elif args.sub == "settings":
        print(json.dumps(get_driver("watch").settings(wrist=args.wrist, crown=args.crown, text_size=args.text_size),
                         indent=1))
    elif args.sub == "model":
        cur = W.current_pair()
        print(f"in use: {W.model_name(cur[0]) if cur else 'none (th watch pair)'}")
        have = {dt["name"] for dt in xcode.watch_devicetypes()}
        by_size = [f"{m.split('(')[-1].rstrip(')')} = {m}" for m in W.SUGGESTED_MODELS if m in have]
        print("by size (what `--model 40mm` and so on pick; one per screen size Trio's Watch app lays out for): "
              + "; ".join(by_size))
        print(f"all: {'; '.join(sorted(have))}")
        print('start one with: th watch pair --model "<model>"')
    elif args.sub == "agent":
        udid = W.watch_udid()
        if args.action == "build":
            print(W.build(udid))
        elif args.action == "start":
            W.pair(); W.start(W.watch_udid()); print("watch agent ready")
        elif args.action == "stop":
            W.stop(); print("stopped")
        elif args.action == "status":
            print("running" if W.is_running() else "not running"); return 0 if W.is_running() else 1
    return 0


def cmd_lock(args):
    target = norm_target(_target(args))
    if target != "sim":
        print("on a phone use `th device lock` / `th device unlock`", file=sys.stderr); return 2
    drv = get_driver(target)
    if args.sub == "status":
        locked = drv.is_locked(); print("locked" if locked else "unlocked"); return 0
    drv.lock() if args.sub == "lock" else drv.unlock()
    print(f"{args.sub}ed" if args.sub == "unlock" else "locked: `shot` now captures the Lock Screen (and Live Activities)")
    return 0


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
        body = Path(args.body).read_text() if args.body else None
        print(post_to_pr(r, args.pr, mode=args.mode, columns=args.columns, max_shots=args.max_shots,
                         artifact_url=args.artifact_url, body=body))
    elif args.sub == "markdown":
        from .report import render_markdown
        md, files = render_markdown(r, columns=args.columns, max_shots=args.max_shots, artifact_url=args.artifact_url)
        print(md); print(f"\n<!-- {len(files)} screenshots would be attached -->")
    return 0


def cmd_webui(args):
    from .webui import serve
    serve(args.host or CONFIG.get("TH_WEBUI_HOST") or "127.0.0.1",
          args.port or int(CONFIG.get("TH_WEBUI_PORT") or 8787), open_browser=args.open, token=args.token)
    return 0


# ---------------- flows / tests -----------------

def cmd_flow(args):
    from .report import Run
    F = _app().flows
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
    elif args.sub in ("dismiss", "settle"):
        print(F.settle(drv, r))
    elif args.sub == "at-home":
        ok = F.at_home(drv); print("home" if ok else "not home"); return 0 if ok else 1
    return 0


def cmd_test(args):
    """Smoke test: launch the app, get to the home screen, add fake CGM + pump, screenshot everything."""
    from .report import Run, render_html
    from .build import BuildInfo
    a = _app()
    F = a.flows
    target = _target(args)
    bi = BuildInfo.load()
    bundle = bi.bundle_id if bi else CONFIG.bundle_id(target)
    r = Run.new(args.name or (f"smoke-{target}" if a.key == "trio" else f"{a.key}-smoke-{target}"),
                {"target": target, "app": a.key, "build": bi.__dict__ if bi else {}, "title": f"{a.name} smoke test ({target})"})
    drv = get_driver(target)
    try:
        if target == "sim":
            drv.boot()
            if not drv.is_installed(bundle):
                raise HarnessError(f"{a.name} is not installed on the simulator; run `th build && th install`")
            if args.preseed:
                F.preseed_simulator(drv, bundle)
            drv.launch(bundle)
        elif target == "rooted-vnc":
            drv.unlock()
            from . import devices as D
            D.ssh_launch(bundle)
        else:
            drv.boot(); drv.open_app_via_spotlight(a.name)
        time.sleep(4)
        F.step(r, drv, "launch", f"{a.name} launched")
        F.dismiss_dialogs(drv, ["OK", "Continue", "Dismiss"])
        if not F.at_home(drv):
            F.walk_onboarding(drv, r)
        F.step(r, drv, "home", "Home screen reached")
        if args.devices:
            F.add_cgm_simulator(drv, r)
            F.back_to_home(drv)
            if target == "sim" and F.give_cgm_glucose(drv, bundle):
                time.sleep(6)
                F.settle(drv, r)
            F.add_pump_simulator(drv, r)
            F.back_to_home(drv)
            time.sleep(3)
            F.step(r, drv, "home-with-devices", f"Home with {F.CGM_SIMULATOR} + {F.PUMP_SIMULATOR}")
        r.finish("passed", f"{a.name} launched and reached the home screen" + (" with fake CGM and pump configured." if args.devices else "."))
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
    from . import apps
    from .faketandem import SCENARIOS
    p = argparse.ArgumentParser(prog="trio-test-harness", description="Trio / Loop test harness toolbox (short alias: th)")
    p.add_argument("--target", help="sim | rooted-vnc | iphone-usb | watch (aliases: device=rooted-vnc, mirror=iphone-usb; default $TH_TARGET or sim)")
    p.add_argument("--app", choices=sorted(apps.APPS),
                   help="the app under test: trio or loop (LoopWorkspace); default $TH_APP, else trio")
    p.add_argument("--git-protocol", choices=["ssh", "https"],
                   help="how git reaches GitHub: ssh (key in ssh-agent) or https (gh CLI login); default $TH_GIT_PROTOCOL or ssh")
    sp = p.add_subparsers(dest="cmd", required=True)

    sp.add_parser("doctor", help="check the environment").set_defaults(fn=cmd_doctor)

    b = sp.add_parser("build", help="check out + build the app (Trio, or Loop with --app loop)")
    b.add_argument("--branch", help="branch | owner/repo@branch | URL#branch")
    b.add_argument("--pr", help="PR number | owner/repo#N | PR URL")
    b.add_argument("--repo", help="GitHub owner/repo to fetch --branch from (default: the checkout's origin)")
    b.add_argument("--trio-branch", help="for submodule PRs: the branch of the app's repo (Trio, LoopWorkspace) that pins "
                                         "that submodule")
    from .build import CHERRY_PICK_HELP
    b.add_argument("--cherry-pick", action="append", metavar="SPEC", help=CHERRY_PICK_HELP)
    from .build import SUBMODULE_CHERRY_PICK_HELP
    b.add_argument("--submodule-cherry-pick", action="append", metavar="SPEC", help=SUBMODULE_CHERRY_PICK_HELP)
    b.add_argument("--in-place", help="build this existing checkout as-is (e.g. ~/repos/Trio or ~/repos/LoopWorkspace with "
                                      "local changes)")
    b.add_argument("--submodule", action="append", default=[], help="NAME@REF | NAME@pr/N | NAME=URL#REF (repeatable)")
    b.add_argument("--configuration", default="Debug")
    b.add_argument("--clean", action="store_true")
    b.add_argument("--trollstore", action="store_true", help="device: ad-hoc sign and package an .ipa for TrollStore")
    b.add_argument("--xcode", action="store_true",
                   help="device build signed with your team (ConfigOverride.xcconfig / TH_DEVELOPER_TEAM), like Xcode; "
                        "for `th install --xcode` on a non-jailbroken iPhone")
    b.add_argument("--no-fetch", action="store_true", help="skip submodule sync")
    b.add_argument("--checkout-only", action="store_true")
    b.set_defaults(fn=cmd_build)

    i = sp.add_parser("install", help="install the last build")
    # not --app: that is th's own option (trio | loop), and a subcommand option of the same name would override it
    i.add_argument("--app-path", help="the .app to install (default: the last build's)"); i.add_argument("--ipa"); i.add_argument("--reinstall", action="store_true")
    i.add_argument("--trollstore", action="store_true"); i.add_argument("--appsync", action="store_true",
                   help="device: install the ad-hoc IPA with pymobiledevice3 (phone has AppSync Unified)")
    i.add_argument("--xcode", action="store_true",
                   help="install a `th build --xcode` build with Xcode's devicectl: any iPhone with Developer Mode, "
                        "unlocked; no jailbreak")
    i.add_argument("--device", help="Xcode installs: UDID or name of the iPhone (default $TH_XCODE_DEVICE, else the "
                                    "only paired iPhone on USB)")
    i.add_argument("--open-on-device", action="store_true",
                                                                          help="type the TrollStore link into Safari via VNC")
    i.add_argument("--serve-seconds", type=int, default=600)
    i.set_defaults(fn=cmd_install)

    l = sp.add_parser("launch", help="launch the app")
    l.add_argument("--bundle-id"); l.add_argument("--preseed-onboarding", action="store_true")
    l.add_argument("--via", choices=["auto", "ssh", "vnc", "devicectl"], default="auto")
    l.add_argument("--app-name", help="the name Spotlight finds it by (default: Trio / Loop)")
    l.set_defaults(fn=cmd_launch)

    ft = sp.add_parser("faketandem", help="the Tandem Mobi emulator a --pump faketandem build talks to: start/stop it, "
                                          "read what the pump holds, make it act (harness/faketandem.py)")
    fts = ft.add_subparsers(dest="sub", required=True)
    fst = fts.add_parser("start", help="start one for this checkout (stopping any running one)")
    fst.add_argument("--setup", choices=["preseed", "walk"], default="preseed",
                     help="preseed: holds a long-term key, for an app seeded as paired (--seed); walk: waits to be "
                          "paired, at the double-tap step")
    fst.add_argument("--scenario", metavar="NAME|FILE", help="pump use replayed before the app connects: a preset ("
                                                             + ", ".join(SCENARIOS) + ") or a scenario JSON file")
    fst.add_argument("--ref", help="faketandem branch/tag/sha to build (default $TH_FAKETANDEM_REF, else main)")
    fst.add_argument("--seed", action="store_true", help="with --setup preseed: seed the installed app as paired")
    fsp = fts.add_parser("stop"); fsp.add_argument("--save", action="store_true",
                                                   help="first keep its state, history, request log and log in the run")
    fsp.add_argument("--run")
    fts.add_parser("seed", help="seed the installed app (simulator) as paired with the running faketandem")
    fss2 = fts.add_parser("status", help="what the pump holds: pairing, basal, IOB, last bolus, reservoir")
    fss2.add_argument("--json", action="store_true", help="faketandem's whole state snapshot")
    fpa = fts.add_parser("pairing", help="print the pairing state, or set it: PairStep1 = the double-tap step, "
                                         "PairStep2 = the pump shows its code, NotDiscoverable")
    fpa.add_argument("state", nargs="?", choices=["NotDiscoverable", "PairStep1", "PairStep2"])
    fhi = fts.add_parser("history", help="the pump's history log: dose records, or --all")
    fhi.add_argument("--all", action="store_true"); fhi.add_argument("--limit", type=int, default=5000)
    fsc = fts.add_parser("scenario", help="replay pump use over its past (refused while the app is connected)")
    fsc.add_argument("scenario", metavar="NAME|FILE", help="a preset (" + ", ".join(SCENARIOS) + ") or a scenario JSON file")
    fap = fts.add_parser("api", help="any request to faketandem's HTTP API, e.g. POST /api/state/bolus/start "
                                     "'{\"units\": 1.5}' (see faketandem's README)")
    fap.add_argument("method", choices=["GET", "POST", "PUT", "PATCH", "DELETE", "get", "post", "put", "patch", "delete"])
    fap.add_argument("path"); fap.add_argument("body", nargs="?")
    ft.set_defaults(fn=cmd_faketandem)

    fi = sp.add_parser("faceid", help="simulator Face ID: enroll a face (trio-test-e2e does at launch), then answer an "
                                      "app's Face ID prompt (a bolus) with match or nomatch")
    fi.add_argument("action", choices=["enroll", "unenroll", "match", "nomatch"]); fi.set_defaults(fn=cmd_faceid)

    dc = sp.add_parser("docs", help="search / browse the app's docs (triodocs.org; with --app loop, LoopDocs) as text: "
                                    "search, toc, page",
                       description=_docs_doc(), formatter_class=argparse.RawDescriptionHelpFormatter)
    dcs = dc.add_subparsers(dest="sub", required=True)
    d1 = dcs.add_parser("search", help="best-matching sections, with links and snippets")
    d1.add_argument("query", nargs="+"); d1.add_argument("-n", "--limit", type=int, default=8)
    d2 = dcs.add_parser("toc", help="every page as a tree"); d2.add_argument("--depth", type=int, help="only this many levels")
    d3 = dcs.add_parser("page", help="a page (or one #section) as text")
    d3.add_argument("ref", help="path (settings/…/) or a URL on the docs site; #anchor for one section")
    dc.set_defaults(fn=cmd_docs)
    bz = sp.add_parser("busy", help="who is driving each target (the device lock; the only thing that counts)",
                       description=_busy_doc(), formatter_class=argparse.RawDescriptionHelpFormatter)
    bz.set_defaults(fn=cmd_busy)
    lg = sp.add_parser("logs", help="Trio / TandemKit / watch log files from the installed app (filter, tail, save); "
                                    "Loop: TandemKit's only",
                       description=_applogs_doc(),
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    lg.add_argument("which", nargs="?", default="all", choices=["trio", "tandem", "watch", "all"],
                    help="which log (default all, merged by time)")
    lg.add_argument("-g", "--grep", metavar="REGEX", help="only entries matching (Python regex; multi-line entries match as a whole)")
    lg.add_argument("-i", "--ignore-case", action="store_true")
    lg.add_argument("--since", metavar="WHEN", help="30s, 10m, 2h, 1d, or an ISO time")
    lg.add_argument("--level", choices=["info", "warning", "error"], help="TandemKit: at least this level")
    lg.add_argument("-n", "--lines", type=int, default=200, help="last N entries (default 200; 0 = all)")
    lg.add_argument("--files", action="store_true", help="list the log files and their sizes")
    lg.add_argument("--save", action="store_true", help="copy the log files into the current run's logs/ (read them with Read/Grep)")
    lg.add_argument("--run", help="run dir for --save (default: $TH_RUN / the current run)")
    lg.add_argument("--bundle-id", help="default: the last build's, else the target's")
    lg.set_defaults(fn=cmd_logs)
    def _debug_parser(name, **kw):
        q = sp.add_parser(name, description=_appdebug_doc(), formatter_class=argparse.RawDescriptionHelpFormatter, **kw)
        q.add_argument("--bundle-id", help="default: the app as installed (simulator) / the last build's (phone)")
        q.add_argument("--run", help="run dir for --save (default: $TH_RUN / the current run)")
        return q
    ol = _debug_parser("oslog", help="the system log of the app's process (all its frameworks): show, record, enable debug")
    ol.add_argument("--since", default="10m", help="30s, 10m, 2h, 1d or an ISO time (default 10m)")
    ol.add_argument("--level", choices=["default", "info", "debug"], help="include info / debug messages (default info; "
                    "--stream: debug)")
    ol.add_argument("--subsystem", help="only this subsystem (prefix), e.g. com.jwoglom.TandemKit")
    ol.add_argument("--all", action="store_true", help="include Apple's frameworks (com.apple.*: UIKit, CoreData, ...)")
    ol.add_argument("-g", "--grep", metavar="REGEX", help="only messages (or categories) matching"); ol.add_argument("-i", "--ignore-case", action="store_true")
    ol.add_argument("-n", "--lines", type=int, default=200, help="last N entries (default 200; 0 = all)")
    ol.add_argument("--stream", type=float, metavar="SECONDS", help="simulator: record the live log this long instead")
    ol.add_argument("--enable-debug", action="store_true", help="simulator: keep the app's and TandemKit's (or "
                    "--subsystem's) debug messages from now on")
    ol.add_argument("--disable-debug", action="store_true", help="simulator: back to the default")
    ol.add_argument("--save", action="store_true", help="write every matching entry into the run's logs/")
    ol.set_defaults(fn=cmd_oslog)
    cr = _debug_parser("crashes", help="the app's crash reports: exception, reason, crashed thread; --save into the run")
    cr.add_argument("--since", help="default: the run's start, else 1d")
    cr.add_argument("--frames", type=int, default=12, help="crashed thread frames to show (default 12)")
    cr.add_argument("--save", action="store_true", help="copy new ones (identifiers removed) into the run's crashes/, "
                    "one run step each")
    cr.set_defaults(fn=cmd_crashes)
    fs = _debug_parser("files", help="files in the app's Documents/ (Trio: OpenAPS JSON state): ls, cat")
    fss = fs.add_subparsers(dest="sub", required=True)
    fss.add_parser("ls", help="list (recursive)").add_argument("path", nargs="?", default="")
    fc = fss.add_parser("cat", help="print one; property lists as JSON"); fc.add_argument("path")
    fc.add_argument("--save", action="store_true", help="write it into the run's files/ instead")
    fs.set_defaults(fn=cmd_files, save=False)
    pf = _debug_parser("perf", help="CPU and memory of the running app; --watch to sample, --leaks (simulator)")
    pf.add_argument("--watch", type=float, metavar="SECONDS", help="keep sampling this long")
    pf.add_argument("--interval", type=float, default=5, help="seconds between samples (default 5)")
    pf.add_argument("--leaks", action="store_true", help="simulator: leaked allocations (`leaks`), by type")
    pf.add_argument("--save", action="store_true", help="append the samples to the run's perf.csv and note them as a step")
    pf.set_defaults(fn=cmd_perf)
    db = sp.add_parser("db", help="the app's local database (Core Data SQLite store) on the simulator: query or change it",
                       description=_appdb_doc(), formatter_class=argparse.RawDescriptionHelpFormatter)
    dbs = db.add_subparsers(dest="sub", required=True)
    dbs.add_parser("stores", help="the SQLite files in the app's containers (* = the app's store)")
    dbs.add_parser("tables", help="tables and row counts")
    dbs.add_parser("schema", help="a table's columns").add_argument("table")
    dq = dbs.add_parser("query", help="run a read-only SELECT"); dq.add_argument("sql")
    dq.add_argument("--json", action="store_true", help="rows as JSON objects")
    de = dbs.add_parser("exec", help="change the store: terminates the app, backs the store up into the run, runs the "
                                     "SQL in one transaction, relaunches")
    de.add_argument("sql"); de.add_argument("--no-relaunch", dest="relaunch", action="store_false")
    de.add_argument("--run", help="run dir for the backup and the step (default: $TH_RUN / the current run)")
    for sub in dbs.choices.values():
        sub.add_argument("--store", help="another store: file name or path ending (`th db stores`)")
        sub.add_argument("--bundle-id", help="default: the last simulator build's, else the app's")
    db.set_defaults(fn=cmd_db)
    s = sp.add_parser("sim", help="simulator management"); ss = s.add_subparsers(dest="sub", required=True)
    for name in ("list", "runtimes", "boot", "shutdown", "erase"):
        ss.add_parser(name)
    ir = ss.add_parser("install-runtime"); ir.add_argument("version", nargs="?")
    ir.add_argument("--platform", default="iOS", choices=["iOS", "watchOS"], help="both apps need both: their schemes embed a Watch app")
    c = ss.add_parser("container"); c.add_argument("--bundle-id")
    d = ss.add_parser("defaults"); d.add_argument("key"); d.add_argument("value"); d.add_argument("--type", default="-string"); d.add_argument("--bundle-id")
    ou = ss.add_parser("openurl"); ou.add_argument("url")
    s.set_defaults(fn=cmd_sim)

    dv = sp.add_parser("device", help="physical iPhone utilities"); ds = dv.add_subparsers(dest="sub", required=True)
    ds.add_parser("list"); ds.add_parser("wake"); ds.add_parser("lock", help="press power: lock the phone to screenshot the Lock Screen / Live Activity"); ds.add_parser("unlock"); ds.add_parser("mirror-status"); ds.add_parser("diag"); ds.add_parser("vnc-restart"); ds.add_parser("apps")
    sshp = ds.add_parser("ssh", help="run a command on the phone over SSH"); sshp.add_argument("cmd", nargs="+"); sshp.add_argument("--timeout", type=int, default=60)
    kp = ds.add_parser("kill"); kp.add_argument("name")
    vc = ds.add_parser("vnc-check"); vc.add_argument("--out")
    si = ds.add_parser("serve-ipa"); si.add_argument("ipa"); si.add_argument("--serve-seconds", type=int, default=600)
    dv.set_defaults(fn=cmd_device)

    dm = sp.add_parser("daemon", help="persistent driver connection reused by all commands"); dms = dm.add_subparsers(dest="sub", required=True)
    st = dms.add_parser("start"); st.add_argument("--foreground", action="store_true")
    for name in ("stop", "status", "restart"):
        dms.add_parser(name)
    dm.set_defaults(fn=cmd_daemon)

    wd = sp.add_parser("wda", help="WebDriverAgent runner for the simulator"); wds = wd.add_subparsers(dest="sub", required=True)
    for name in ("build", "start", "stop", "status"):
        wds.add_parser(name)
    wd.set_defaults(fn=cmd_wda)

    sh = sp.add_parser("shot", help="screenshot into the current run"); sh.add_argument("--name"); sh.add_argument("--note")
    sh.add_argument("--out"); sh.add_argument("--run"); sh.add_argument("--ocr", action="store_true"); sh.set_defaults(fn=cmd_shot)

    t = sp.add_parser("tap"); t.add_argument("x", type=int); t.add_argument("y", type=int); t.add_argument("--duration", type=float, default=0); t.set_defaults(fn=cmd_tap)
    sw = sp.add_parser("swipe"); [sw.add_argument(n, type=int) for n in ("x1", "y1", "x2", "y2")]; sw.add_argument("--duration", type=float, default=0.3); sw.set_defaults(fn=cmd_swipe)
    dg = sp.add_parser("drag", help="long-press then move (SwiftUI drag & drop / reorder)"); [dg.add_argument(n, type=int) for n in ("x1", "y1", "x2", "y2")]
    dg.add_argument("--hold", type=float, default=0.8); dg.add_argument("--duration", type=float, default=0.8); dg.add_argument("--settle", type=float, default=0.5); dg.set_defaults(fn=cmd_drag)
    sc = sp.add_parser("scroll"); sc.add_argument("direction", choices=["up", "down", "left", "right"]); sc.add_argument("--amount", type=float, default=0.5); sc.set_defaults(fn=cmd_scroll)
    ty = sp.add_parser("type"); ty.add_argument("text"); ty.set_defaults(fn=cmd_type)
    k = sp.add_parser("key"); k.add_argument("name"); k.set_defaults(fn=cmd_key)
    sp.add_parser("home").set_defaults(fn=cmd_home)
    cr = sp.add_parser("crown", help="--target watch: turn the Digital Crown (1.0 = one full turn, negative = the other way)")
    cr.add_argument("delta", type=float); cr.add_argument("--velocity", type=float, help="turns per second")
    cr.set_defaults(fn=cmd_crown)
    wt = sp.add_parser("watch", help="watch simulator paired with the sim, and its input agent (harness/watch.py)")
    wts = wt.add_subparsers(dest="sub", required=True)
    def watch_settings_args(sp_):
        sp_.add_argument("--wrist", choices=["left", "right"])
        sp_.add_argument("--crown", choices=["left", "right"], help="the side the Digital Crown is on")
        sp_.add_argument("--text-size", metavar="SIZE", help="default, extra-small … extra-extra-extra-large, "
                         "accessibility-medium … accessibility-extra-extra-extra-large")
    wpr = wts.add_parser("pair", help="start the phone + watch simulator pair for a watch model (each model has its own "
                                      "phone, created on first use): shuts down the pair in use, boots this one, installs "
                                      "the last build where missing, launches the Watch app and then the phone app; every "
                                      "command then drives this pair")
    wpr.add_argument("--model", help='watch size or model: 40mm, 41mm, 42mm, 44mm, 45mm, 46mm, 49mm (or ultra), or a name '
                                     'from `th watch model` (default: the pair in use, else TH_WATCH_SIM_DEVICE)')
    wpr.add_argument("--recreate", action="store_true", help="delete the pair's two simulators and make new ones (their "
                                                              "apps and data go)")
    watch_settings_args(wpr)
    wts.add_parser("status")
    ws = wts.add_parser("settings", help="wrist, crown side and text size; with none given, prints them. The Watch app "
                                         "in front is relaunched to read them (no simulator restart)")
    watch_settings_args(ws)
    wts.add_parser("model", help="list the watch models (start one with `th watch pair --model`)")
    wa = wts.add_parser("agent", help="the XCUITest agent that taps, reads the tree and turns the crown")
    wa.add_argument("action", choices=["build", "start", "stop", "status"])
    wt.set_defaults(fn=cmd_watch)
    for name, helptext in (("lock", "lock the simulator: `shot` then captures the Lock Screen and Live Activities"),
                           ("unlock", "unlock the simulator after `lock`")):
        sp.add_parser(name, help=helptext).set_defaults(fn=cmd_lock, sub=name)
    sp.add_parser("locked", help="print whether the simulator is locked").set_defaults(fn=cmd_lock, sub="status")
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
    rpr.add_argument("--artifact-url", help="claude.ai artifact link to include in the comment")
    rpr.add_argument("--body", metavar="FILE", help="post this markdown instead of the generated summary; the run's "
                     "screenshots it shows as ![caption](./screenshots/NNN.png) are attached")
    rm = rps.add_parser("markdown", help="preview the PR comment body"); rm.add_argument("--run"); rm.add_argument("--columns", type=int, default=3)
    rm.add_argument("--max-shots", type=int); rm.add_argument("--artifact-url")
    rp.set_defaults(fn=cmd_report)

    wu = sp.add_parser("webui", help="browser UI: run the e2e job (the CI workflow's options) on this machine; "
                                     "post the PR comment only when asked")
    wu.add_argument("--host", help="bind address, e.g. 0.0.0.0 for other machines (default $TH_WEBUI_HOST, else 127.0.0.1)")
    wu.add_argument("--port", type=int, help="default $TH_WEBUI_PORT, else 8787")
    wu.add_argument("--open", action="store_true", help="open the page in the default browser")
    wu.add_argument("--token", "--password", "-p", dest="token", metavar="TOKEN",
                    help="access token for the page (default: a random one kept in runs/.tmp/webui-token)")
    wu.set_defaults(fn=cmd_webui)

    fl = sp.add_parser("flow", help="the app's UI flows (harness/flows/trio.py, loop.py)"); fls = fl.add_subparsers(dest="sub", required=True)
    for name in ("onboarding-walk", "add-cgm-sim", "add-pump-sim", "back-home", "dismiss", "settle", "at-home"):
        x = fls.add_parser(name); x.add_argument("--run")
    fl.set_defaults(fn=cmd_flow)

    te = sp.add_parser("test", help="end-to-end smoke test on the installed build")
    te.add_argument("kind", choices=["smoke"]); te.add_argument("--name"); te.add_argument("--no-preseed", dest="preseed", action="store_false")
    te.add_argument("--no-devices", dest="devices", action="store_false"); te.set_defaults(fn=cmd_test)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.git_protocol:
        CONFIG.set("TH_GIT_PROTOCOL", args.git_protocol)
    try:
        from . import apps
        apps.select(args.app)
        return args.fn(args) or 0
    except HarnessError as e:
        print(f"th: error: {e}", file=sys.stderr)
        return 2
    except LookupError as e:
        print(f"th: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
