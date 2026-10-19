"""WatchDriver (`--target watch`): drive the Watch app on a watchOS simulator paired with the harness's iPhone simulator.

    th watch pair                  # create TrioHarnessWatch, pair it with TrioHarness, boot both
    th watch agent start           # build (once) and start the input agent on the watch
    th --target watch install      # the last build's embedded Watch app (Trio.app/Watch/*.app)
    th --target watch launch       # the Watch app, by the last build's bundle id + .watchkitapp
    th --target watch shot | tap X Y | tap-text "Bolus" | tree | crown 0.5 | home

Screenshots come from `simctl io`. Input and the accessibility tree come from the agent in harness/watchagent: a
watchOS XCUITest bundle whose one test never ends, run with `xcodebuild test-without-building` as WebDriverAgent is on
the phone (Appium's WebDriverAgent has no watchOS runner). The harness talks to it through files in
runs/.tmp/watch-agent: cmd-<id>.json in, res-<id>.json out. XCUITest is what turns the Digital Crown
(`XCUIDevice.rotateDigitalCrown`, where a delta of 1.0 is one full turn).

The watch shares the simulator's device lock (`th busy`): it is part of the same paired run.
"""
from __future__ import annotations

import glob
import json
import plistlib
import re
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

from .config import CONFIG, REPO_ROOT
from .driver import Driver, UINode
from .util import HarnessError, log, run, wait_for
from . import xcode

AGENT_SRC = Path(__file__).resolve().parent / "watchagent"
AGENT_DD = REPO_ROOT / "build" / "watchagent-dd"
AGENT_DIR = REPO_ROOT / "runs" / ".tmp" / "watch-agent"
PID_FILE = REPO_ROOT / "runs" / ".tmp" / "watch-agent.pid"
LOG_FILE = REPO_ROOT / "runs" / ".tmp" / "watch-agent.log"
APP_FILE = REPO_ROOT / "runs" / ".tmp" / "watch-app.txt"     # the Watch app last launched, for a restarted agent
CURRENT_FILE = REPO_ROOT / "runs" / ".tmp" / "watch-udid.txt"  # the watch in use (`th watch model`), for every process
AGENT_UDID_FILE = REPO_ROOT / "runs" / ".tmp" / "watch-agent-udid.txt"
HOME_FLAG = REPO_ROOT / "runs" / ".tmp" / "watch-at-home"      # `th --target watch home`: leave the clock in front
CAROUSEL = "com.apple.Carousel"

# Settings ▸ General ▸ Orientation and Settings ▸ Display & Brightness ▸ Text Size, as the watch stores them. An app
# reads them when it starts, so a change takes a relaunch of the app (not of the simulator). simctl's own
# `ui content_size` refuses a watchOS runtime ("does not support dynamic text").
ORIENTATION_DOMAIN = "com.apple.nano"
WRIST_KEY = "wornOnRightArm"      # WKInterfaceDevice.wristLocation: right when true
CROWN_KEY = "invertUI"            # WKInterfaceDevice.crownOrientation: left when true, whichever the wrist
TEXT_DOMAIN, TEXT_KEY = "com.apple.UIKit", "UIPreferredContentSizeCategoryName"
TEXT_SIZES = {"extra-small": "XS", "small": "S", "medium": "M", "large": "L", "extra-large": "XL",
              "extra-extra-large": "XXL", "extra-extra-extra-large": "XXXL",
              "accessibility-medium": "AccessibilityM", "accessibility-large": "AccessibilityL",
              "accessibility-extra-large": "AccessibilityXL", "accessibility-extra-extra-large": "AccessibilityXXL",
              "accessibility-extra-extra-extra-large": "AccessibilityXXXL"}
# One model per screen size Trio's Watch app tells apart (WatchSize in its Helper+Enums.swift), plus the newest 46 mm,
# which matches none of its sizes and gets its `.unknown` layout.
SUGGESTED_MODELS = ["Apple Watch SE 3 (40mm)", "Apple Watch Series 9 (41mm)", "Apple Watch Series 11 (42mm)",
                    "Apple Watch SE 3 (44mm)", "Apple Watch Series 9 (45mm)", "Apple Watch Series 11 (46mm)",
                    "Apple Watch Ultra 3 (49mm)"]


# ---------------- simulators -----------------

def resolve_model(name: str | None) -> str:
    """A watch model from a size ("40mm", "40 mm", "40": that size's model in SUGGESTED_MODELS), "ultra", or a model name
    in any case; None gives TH_WATCH_SIM_DEVICE."""
    name = (name or CONFIG.get("TH_WATCH_SIM_DEVICE")).strip()
    names = [dt["name"] for dt in xcode.watch_devicetypes()]
    for n in names:
        if n.lower() == name.lower():
            return n
    m = re.fullmatch(r"(\d{2})\s*(mm)?", name.lower())
    if m:
        for n in SUGGESTED_MODELS:
            if f"({m.group(1)}mm)" in n and n in names:
                return n
    if name.lower() == "ultra":
        return next(n for n in SUGGESTED_MODELS if "Ultra" in n)
    sizes = ", ".join(re.search(r"\((\d+mm)\)", n).group(1) for n in SUGGESTED_MODELS)
    raise HarnessError(f"unknown watch model {name!r}: give a size ({sizes}, or ultra) or a name from `th watch model`")


def pair_names(model: str) -> tuple[str, str]:
    """The harness's phone and watch simulator names for a watch model."""
    return f"{CONFIG.get('TH_SIM_NAME')} ({model})", f"{CONFIG.get('TH_WATCH_SIM_NAME')} ({model})"


def ensure_pair(model: str | None = None, *, recreate: bool = False) -> tuple[str, str]:
    """The phone + watch simulator pair for a watch model (default TH_WATCH_SIM_DEVICE), created on first use, and
    made the pair in use for every harness command (the phone too). Returns (watch udid, phone udid); boots neither.

    Each model gets a phone of its own, paired once, at creation, while both are shut down. A phone simulator that has
    been paired again (another watch, `pair_activate`, `unpair`) or erased stops linking Watch apps to their iPhone apps
    (its wcd logs `iOSApplicationsWithWatchAppInstalled: {()}`, so WCSession.isWatchAppInstalled is false), and nothing
    short of a new simulator brought it back (docs/HANDOFF.md)."""
    model = resolve_model(model)
    phone_name, watch_name = pair_names(model)
    devices = [d for d in xcode.list_devices() if d.get("isAvailable", True)]
    phones = {d["udid"] for d in devices if d["name"] == phone_name}
    watches = {d["udid"] for d in devices if d["name"] == watch_name}
    found = None if recreate else next(
        ((p["watch"]["udid"], p["phone"]["udid"]) for p in xcode.list_pairs().values()
         if p["watch"]["udid"] in watches and p["phone"]["udid"] in phones), None)
    if not found:
        for udid in phones | watches:           # this harness's own, half-made or broken pair
            xcode.xcrun(["simctl", "shutdown", udid], check=False, quiet=True)
            xcode.xcrun(["simctl", "delete", udid], quiet=True)
        log(f"creating the phone + watch simulator pair for {model} (first boot takes minutes)")
        phone = xcode.create_device(phone_name, CONFIG.get("TH_SIM_DEVICE"),
                                    xcode.pick_runtime(CONFIG.get("TH_SIM_RUNTIME")))["udid"]
        watch = xcode.create_device(watch_name, model,
                                    xcode.pick_runtime(CONFIG.get("TH_WATCH_SIM_RUNTIME"), "watchOS"))["udid"]
        xcode.xcrun(["simctl", "pair", watch, phone])
        found = (watch, phone)
    CURRENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.write_text(found[0])
    xcode.SIM_CURRENT_FILE.write_text(found[1])
    return found


def current_pair() -> tuple[str, str] | None:
    """(watch, phone) of the pair in use, or None before `th watch pair`."""
    if not CURRENT_FILE.exists() or not xcode.SIM_CURRENT_FILE.exists():
        return None
    w, ph = CURRENT_FILE.read_text().strip(), xcode.SIM_CURRENT_FILE.read_text().strip()
    for p in xcode.list_pairs().values():
        if p["watch"]["udid"] == w and p["phone"]["udid"] == ph:
            return w, ph
    return None


def watch_udid() -> str:
    """The watch of the pair in use, or the default model's pair (created on first use)."""
    return (current_pair() or ensure_pair())[0]


def model_name(udid: str) -> str:
    d = next((d for d in xcode.list_devices() if d["udid"] == udid), {})
    dtid = d.get("deviceTypeIdentifier", "")
    return next((dt["name"] for dt in xcode.list_devicetypes() if dt["identifier"] == dtid), d.get("name", udid))


def read_settings(udid: str) -> dict:
    def get(domain, key):
        cp = xcode.xcrun(["simctl", "spawn", udid, "defaults", "read", domain, key], check=False, quiet=True)
        return cp.stdout.strip() if cp.returncode == 0 else None
    size = get(TEXT_DOMAIN, TEXT_KEY)
    by_value = {f"UICTContentSizeCategory{v}": k for k, v in TEXT_SIZES.items()}
    return {"wrist": "right" if get(ORIENTATION_DOMAIN, WRIST_KEY) == "1" else "left",
            "crown": "left" if get(ORIENTATION_DOMAIN, CROWN_KEY) == "1" else "right",
            "text_size": by_value.get(size, size) if size else "default"}


def write_settings(udid: str, *, wrist: str | None = None, crown: str | None = None,
                   text_size: str | None = None) -> bool:
    """Write the watch's orientation and text size; returns whether anything was given. Apps see it on their next
    launch."""
    def spawn(*a):
        xcode.xcrun(["simctl", "spawn", udid, *a], quiet=True)
    if wrist:
        if wrist not in ("left", "right"):
            raise HarnessError("--wrist is left or right")
        spawn("defaults", "write", ORIENTATION_DOMAIN, WRIST_KEY, "-bool", "true" if wrist == "right" else "false")
        spawn("notifyutil", "-p", "com.apple.nano.wristOrientationChangedNotification")
    if crown:
        if crown not in ("left", "right"):
            raise HarnessError("--crown is left or right")
        spawn("defaults", "write", ORIENTATION_DOMAIN, CROWN_KEY, "-bool", "true" if crown == "left" else "false")
        spawn("notifyutil", "-p", "com.apple.nano.invertUIChangedNotification")
    if text_size:
        if text_size == "default":
            xcode.xcrun(["simctl", "spawn", udid, "defaults", "delete", TEXT_DOMAIN, TEXT_KEY], check=False, quiet=True)
        elif text_size in TEXT_SIZES:
            spawn("defaults", "write", TEXT_DOMAIN, TEXT_KEY, f"UICTContentSizeCategory{TEXT_SIZES[text_size]}")
        else:
            raise HarnessError(f"--text-size is default or one of: {', '.join(TEXT_SIZES)}")
    return bool(wrist or crown or text_size)


def pair(model: str | None = None, *, recreate: bool = False) -> tuple[str, str]:
    """The pair for `model` (or the one in use, else the default model's), booted: phone first, then the watch.
    Returns (watch udid, phone udid)."""
    watch, phone = ensure_pair(model, recreate=recreate) if (model or recreate or not current_pair()) else current_pair()
    xcode.boot_simulator(phone)
    xcode.boot_simulator(watch)
    return watch, phone


def watch_bundle_id(bundle_id: str) -> str:
    """The Watch app's bundle id for a phone app's: read from the last build's embedded Watch app when it is that app's
    companion (its WKCompanionAppBundleIdentifier), else the phone app's id + the app's suffix (harness/apps.py:
    Trio .watchkitapp, Loop .LoopWatch). A watch app's own id, or a system app's, is returned as it is."""
    if bundle_id == CAROUSEL or bundle_id.startswith("com.apple."):
        return bundle_id
    from . import apps
    from .build import BuildInfo
    bi = BuildInfo.load()
    if bi and bi.app_path and Path(bi.app_path).exists():
        try:
            info = plistlib.loads((embedded_watch_app(Path(bi.app_path)) / "Info.plist").read_bytes())
            if bundle_id in (info.get("CFBundleIdentifier"), info.get("WKCompanionAppBundleIdentifier")):
                return info["CFBundleIdentifier"]
        except (HarnessError, OSError, KeyError, plistlib.InvalidFileException):
            pass
    suffixes = {a.watch_suffix for a in apps.APPS.values()}
    if bundle_id.endswith(tuple(suffixes)):
        return bundle_id
    return f"{bundle_id}{apps.current().watch_suffix}"


def embedded_watch_app(app: Path) -> Path:
    """The Watch app inside a built iOS app, or `app` itself when it already is one."""
    if app.parent.name == "Watch" or not (app / "Watch").is_dir():
        return app
    hits = sorted((app / "Watch").glob("*.app"))
    if not hits:
        raise HarnessError(f"{app.name} embeds no Watch app")
    return hits[0]


# ---------------- agent -----------------

def xctestrun() -> Path | None:
    hits = sorted(glob.glob(str(AGENT_DD / "Build" / "Products" / "*watchsimulator*.xctestrun")))
    return Path(hits[-1]) if hits else None


def build(udid: str) -> Path:
    log("building the watch agent (harness/watchagent) for the watch simulator (one-off)")
    run(["xcodebuild", "build-for-testing", "-project", str(AGENT_SRC / "THWatchAgent.xcodeproj"),
         "-scheme", "THWatchAgent", "-destination", f"id={udid}", "-derivedDataPath", str(AGENT_DD),
         "CODE_SIGNING_ALLOWED=NO", "COMPILER_INDEX_STORE_ENABLE=NO", "-quiet"],
        env=xcode.developer_env(), timeout=3600, capture=False)
    p = xctestrun()
    if not p:
        raise HarnessError("the watch agent build produced no .xctestrun")
    return p


def _pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def is_running(max_age: float = 5.0) -> bool:
    """The agent answers: its xcodebuild is alive and it wrote its heartbeat within `max_age` seconds."""
    alive = AGENT_DIR / "alive.json"
    return _pid() is not None and alive.exists() and time.time() - alive.stat().st_mtime < max_age


def start(udid: str, timeout: float = 900) -> None:
    on = AGENT_UDID_FILE.read_text().strip() if AGENT_UDID_FILE.exists() else None
    if is_running() and on == udid:
        return
    stop()
    xr = xctestrun() or build(udid)
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    for f in AGENT_DIR.glob("*.json"):
        f.unlink()
    env = {**os.environ, **xcode.developer_env(), "TEST_RUNNER_TH_WATCH_AGENT_DIR": str(AGENT_DIR)}
    cmd = ["xcodebuild", "test-without-building", "-xctestrun", str(xr), "-destination", f"id={udid}",
           "-derivedDataPath", str(AGENT_DD)]
    log(f"starting the watch agent on {udid} (log: {LOG_FILE})")
    with open(LOG_FILE, "w") as fh:
        p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    PID_FILE.write_text(str(p.pid))
    AGENT_UDID_FILE.write_text(udid)
    try:
        wait_for(lambda: is_running() or p.poll() is not None, timeout, 1.0, what="the watch agent")
    except HarnessError:
        pass
    if not is_running():
        tail = LOG_FILE.read_text()[-2000:] if LOG_FILE.exists() else ""
        raise HarnessError(f"the watch agent did not come up\n{tail}")
    if APP_FILE.exists():
        call("focus", bundleId=APP_FILE.read_text().strip())


def stop() -> None:
    pid = _pid()
    if pid:
        try:
            call("quit", timeout=10)
        except HarnessError:
            pass
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    PID_FILE.unlink(missing_ok=True)


def call(method: str, timeout: float = 120, **params):
    """One command to the agent; returns its result or raises with the agent's error (an XCUITest issue)."""
    if not (AGENT_DIR / "alive.json").exists():
        raise HarnessError("the watch agent is not running: `th watch agent start`")
    cid = f"{time.time_ns()}-{uuid.uuid4().hex[:6]}"
    tmp, cmd, res = AGENT_DIR / f".cmd-{cid}.tmp", AGENT_DIR / f"cmd-{cid}.json", AGENT_DIR / f"res-{cid}.json"
    tmp.write_text(json.dumps({"method": method, "params": params}))
    tmp.rename(cmd)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if res.exists():
            try:
                data = json.loads(res.read_text())
            except ValueError:
                time.sleep(0.05)
                continue
            res.unlink(missing_ok=True)
            if not data.get("ok"):
                raise HarnessError(f"watch agent {method}: {data.get('error')}")
            return data.get("result")
        if not is_running(max_age=30):
            cmd.unlink(missing_ok=True)
            raise HarnessError(f"the watch agent stopped while running {method} (log: {LOG_FILE})")
        time.sleep(0.05)
    cmd.unlink(missing_ok=True)
    raise HarnessError(f"watch agent {method}: no answer in {timeout:.0f} s")


# ---------------- driver -----------------

class WatchDriver(Driver):
    kind = "watch"

    def __init__(self, udid: str | None = None):
        self._udid = udid or watch_udid()
        self._points: tuple[float, float] | None = None

    @property
    def udid(self) -> str:
        # follows `th watch model` from any process, a daemon's driver included
        if CURRENT_FILE.exists() and (cur := CURRENT_FILE.read_text().strip()) and cur != self._udid:
            self._udid, self._points = cur, None
        return self._udid

    @udid.setter
    def udid(self, value: str) -> None:
        self._udid, self._points = value, None

    def _simctl(self, args: list[str], **kw):
        return xcode.xcrun(["simctl", *args], **kw)

    # ---- lifecycle ----
    def boot(self) -> None:
        pair()

    def settings(self, *, wrist: str | None = None, crown: str | None = None, text_size: str | None = None) -> dict:
        """Change the watch's wrist, crown side or text size (None leaves one as it is) and relaunch the Watch app in
        front, which reads them at launch; the simulator keeps running. Returns the settings now in force."""
        if write_settings(self.udid, wrist=wrist, crown=crown, text_size=text_size) and APP_FILE.exists() \
                and is_running() and call("state")["state"] == "foreground":
            call("launch", bundleId=APP_FILE.read_text().strip())
        return {"model": model_name(self.udid), **read_settings(self.udid)}

    def use_model(self, device: str | None = None, *, app_path: str | None = None, bundle_id: str | None = None,
                  wrist: str | None = None, crown: str | None = None, text_size: str | None = None,
                  recreate: bool = False) -> dict:
        """Switch to the phone + watch pair of watch model `device` (both created on first use, minutes): boot it,
        install `app_path` on the phone and then its Watch app on the watch where missing, apply the settings, move
        the watch agent and WebDriverAgent to it, launch the Watch app and then the phone app (onboarding pre-seeded).
        It is another phone simulator: its app keeps its own data, so a new pair has no fake CGM or pump yet."""
        prev = current_pair()
        target = ensure_pair(device, recreate=recreate)
        if prev and prev != target:
            # the pair left behind keeps its state; shut down, it does not compete with the new pair's (first) boot
            stop()
            from . import wda
            wda.stop()
            for udid in prev:
                xcode.xcrun(["simctl", "shutdown", udid], check=False, quiet=True)
        watch, phone = pair(device)
        self.udid = watch
        write_settings(watch, wrist=wrist, crown=crown, text_size=text_size)
        from .sim import SimulatorDriver
        from . import apps, wda
        phone_drv = SimulatorDriver(phone)
        if app_path and bundle_id:
            # the phone app before its Watch app, as on every pair that linked them
            if not phone_drv.is_installed(bundle_id):
                phone_drv.install(Path(app_path))
            if not self.is_installed(bundle_id):
                self.install(Path(app_path))
            apps.current().flows.preseed_simulator(phone_drv, bundle_id)
        start(watch)
        if wda.is_running():
            wda.start(phone)
        if bundle_id:
            self.launch(bundle_id)
            phone_drv.launch(bundle_id)
            # A new pair's phone links its apps to their Watch apps seconds after the first launch, which then finds no
            # watch (isWatchAppInstalled false); a relaunch a little later does.
            time.sleep(30)
            phone_drv.launch(bundle_id)
        return {"watch": watch, "phone": phone, "model": model_name(watch), **read_settings(watch)}

    def install(self, app_path: Path) -> None:
        # Uninstalling the phone app on a paired phone also deletes its Watch app, and that deletion reaches the watch
        # seconds later, so a Watch app installed meanwhile vanishes (seen 3 s after its install): install until it stays.
        watch_app = embedded_watch_app(Path(app_path))
        bid = plistlib.loads((watch_app / "Info.plist").read_bytes())["CFBundleIdentifier"]
        for attempt in range(3):
            self._simctl(["install", self.udid, str(watch_app)])
            time.sleep(10)
            if self.is_installed(bid):
                self._restart_wcd()
                return
            log(f"{bid} was removed from the watch right after its install (the phone app's uninstall); installing again")
        raise HarnessError(f"{bid} does not stay installed on the watch")

    def _restart_wcd(self) -> None:
        """After a Watch app is reinstalled, the watch's WatchConnectivity daemon can keep a stale record of it: it then
        files a WatchKit extension's session under the extension's own id instead of its iPhone app's (Loop's
        `…LoopWatch.watchkitextension` instead of `…Loop`), and the phone's context never reaches it. A restarted wcd
        (launchd starts it again on demand) reads the apps afresh."""
        import signal
        for parent in run(["pgrep", "-f", f"launchd_sim.*{self.udid}"], check=False, quiet=True).stdout.split():
            for pid in run(["pgrep", "-P", parent, "-x", "wcd"], check=False, quiet=True).stdout.split():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass

    def uninstall(self, bundle_id: str) -> None:
        self._simctl(["uninstall", self.udid, watch_bundle_id(bundle_id)], check=False)

    def is_installed(self, bundle_id: str) -> bool:
        cp = self._simctl(["get_app_container", self.udid, watch_bundle_id(bundle_id)], check=False, quiet=True)
        return cp.returncode == 0

    def launch(self, bundle_id: str, *, terminate_first: bool = True) -> None:
        bid = watch_bundle_id(bundle_id)
        APP_FILE.parent.mkdir(parents=True, exist_ok=True)
        APP_FILE.write_text(bid)
        HOME_FLAG.unlink(missing_ok=True)
        # simctl starts a watch app suspended, and a suspended Watch app is not reachable from the phone, so the agent
        # brings it to the front. Not with XCUIApplication.launch: a launch it cannot complete fails the test, which
        # is the agent (seen right after a watch was erased).
        if terminate_first:
            self._simctl(["terminate", self.udid, bid], check=False, quiet=True)
        self._simctl(["launch", self.udid, bid], quiet=True)
        if is_running():
            st = call("activate", bundleId=bid)
            if st.get("state") != "foreground":
                log(f"the Watch app is {st.get('state')} after activate; the phone may not reach it")
        else:
            log("the watch agent is not running: the Watch app may stay suspended (`th watch agent start`)")

    def terminate(self, bundle_id: str) -> None:
        bid = watch_bundle_id(bundle_id)
        if is_running():
            call("terminate", bundleId=bid)
        else:
            self._simctl(["terminate", self.udid, bid], check=False)

    def app_data_container(self, bundle_id: str) -> Path:
        return xcode.app_container(self.udid, watch_bundle_id(bundle_id), "data")

    def _keep_front(self) -> None:
        """watchOS returns to the clock after 2 minutes without a touch, which would put the next screenshot or tap on
        the watch face; bring the Watch app the harness launched back, unless `home` put the clock there on purpose."""
        if HOME_FLAG.exists() or not APP_FILE.exists() or not is_running():
            return
        bid = APP_FILE.read_text().strip()
        if call("state", bundleId=bid).get("state") != "foreground":
            log("the Watch app had gone back to the clock (watchOS does after 2 min idle); bringing it to the front")
            call("activate", bundleId=bid)
            time.sleep(1)

    # ---- screen (screenshot pixels; the agent works in points) ----
    def screenshot(self, path: Path) -> Path:
        self._keep_front()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._simctl(["io", self.udid, "screenshot", "--type=png", str(path)], quiet=True)
        return path

    def screen_size(self) -> tuple[int, int]:
        from PIL import Image
        from .util import tmp_path
        with Image.open(self.screenshot(tmp_path("watch-size.png"))) as im:
            return im.width, im.height

    @property
    def scale(self) -> float:
        if self._points is None:
            s = call("screen")
            self._points = (float(s["width"]), float(s["height"]))
        w, _ = self.screen_size()
        return w / self._points[0]

    def _pt(self, x: float, y: float) -> tuple[float, float]:
        s = self.scale
        return x / s, y / s

    # ---- input (pixel coords) ----
    def tap(self, x: int, y: int, *, duration: float = 0.0) -> None:
        self._keep_front()
        px, py = self._pt(x, y)
        if duration:
            call("press", x=px, y=py, duration=duration)
        else:
            call("tap", x=px, y=py)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, *, duration: float = 0.3) -> None:
        (a, b), (c, d) = self._pt(x1, y1), self._pt(x2, y2)
        call("drag", x1=a, y1=b, x2=c, y2=d, hold=0.05, velocity=max(200.0, ((c - a) ** 2 + (d - b) ** 2) ** 0.5 / max(duration, 0.05)))

    def drag(self, x1: int, y1: int, x2: int, y2: int, *, hold: float = 0.8, duration: float = 0.8, settle: float = 0.5) -> None:
        (a, b), (c, d) = self._pt(x1, y1), self._pt(x2, y2)
        call("drag", x1=a, y1=b, x2=c, y2=d, hold=hold, settle=settle,
             velocity=max(50.0, ((c - a) ** 2 + (d - b) ** 2) ** 0.5 / max(duration, 0.05)))

    def crown(self, delta: float, *, velocity: float | None = None) -> None:
        """Turn the Digital Crown: 1.0 is one full turn; negative turns the other way."""
        self._keep_front()
        if velocity is None:
            call("crown", delta=delta)
        else:
            call("crown", delta=delta, velocity=velocity)

    def type_text(self, text: str) -> None:
        call("type", text=text)

    def key(self, name: str) -> None:
        raise HarnessError("the watch has no keyboard keys; use tap, crown or type")

    def home(self) -> None:
        call("home")
        HOME_FLAG.touch()

    def wake(self) -> None:
        pass

    def state(self, bundle_id: str | None = None) -> dict:
        return call("state", **({"bundleId": watch_bundle_id(bundle_id)} if bundle_id else {}))

    def tap_text(self, text: str, *, exact: bool = False, index: int = 0, ocr_fallback: bool = True,
                 screenshot_for_ocr: Path | None = None) -> tuple[int, int]:
        """Tap the matching text or icon rather than the centre of the row or button around it: a SwiftUI List row's
        button on watchOS takes touches only on its content (Trio's treatment menu rows ignore a tap between icon and
        label), so the centre of a wide row can miss."""
        nodes = [n for n in self.find(text, exact=exact) if n.w > 0 and n.h > 0]
        content = [n for n in nodes if n.role.endswith(("StaticText", "Image"))]
        nodes = content or sorted(nodes, key=lambda n: n.w * n.h)
        if nodes:
            x, y = nodes[index if -len(nodes) <= index < len(nodes) else -1].center
            self.tap(x, y)
            return x, y
        return super().tap_text(text, exact=exact, index=index, ocr_fallback=ocr_fallback,
                                screenshot_for_ocr=screenshot_for_ocr)

    # ---- accessibility tree ----
    def ui_tree(self) -> list[UINode]:
        self._keep_front()
        s = self.scale
        return [UINode(label=n.get("label") or "", value=n.get("value") or "", identifier=n.get("identifier") or "",
                       role=n.get("role") or "", enabled=bool(n.get("enabled", True)),
                       x=int(n["x"] * s), y=int(n["y"] * s), w=int(n["w"] * s), h=int(n["h"] * s))
                for n in call("tree")]
