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
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

from .config import REPO_ROOT
from .driver import Driver, UINode
from .util import HarnessError, log, run, wait_for
from . import xcode

AGENT_SRC = Path(__file__).resolve().parent / "watchagent"
AGENT_DD = REPO_ROOT / "build" / "watchagent-dd"
AGENT_DIR = REPO_ROOT / "runs" / ".tmp" / "watch-agent"
PID_FILE = REPO_ROOT / "runs" / ".tmp" / "watch-agent.pid"
LOG_FILE = REPO_ROOT / "runs" / ".tmp" / "watch-agent.log"
APP_FILE = REPO_ROOT / "runs" / ".tmp" / "watch-app.txt"     # the Watch app last launched, for a restarted agent
CAROUSEL = "com.apple.Carousel"


# ---------------- simulators -----------------

def watch_udid() -> str:
    return xcode.ensure_watch_simulator()["udid"]


def pair() -> tuple[str, str]:
    """Create the watch simulator if needed, pair it with the harness's iPhone simulator, boot both.
    Returns (watch udid, phone udid)."""
    phone = xcode.ensure_simulator()["udid"]
    watch = watch_udid()
    xcode.ensure_pair(watch, phone)
    xcode.boot_simulator(phone)
    xcode.boot_simulator(watch)
    return watch, phone


def watch_bundle_id(bundle_id: str) -> str:
    """The Watch app's bundle id for an app's (the phone app's id + .watchkitapp, Trio's PRODUCT_BUNDLE_IDENTIFIER)."""
    if bundle_id == CAROUSEL or bundle_id.endswith(".watchkitapp") or bundle_id.startswith("com.apple."):
        return bundle_id
    return f"{bundle_id}.watchkitapp"


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
    if is_running():
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
        self.udid = udid or watch_udid()
        self._points: tuple[float, float] | None = None

    def _simctl(self, args: list[str], **kw):
        return xcode.xcrun(["simctl", *args], **kw)

    # ---- lifecycle ----
    def boot(self) -> None:
        pair()

    def ensure_agent(self) -> None:
        start(self.udid)

    def install(self, app_path: Path) -> None:
        self._simctl(["install", self.udid, str(embedded_watch_app(Path(app_path)))])

    def uninstall(self, bundle_id: str) -> None:
        self._simctl(["uninstall", self.udid, watch_bundle_id(bundle_id)], check=False)

    def is_installed(self, bundle_id: str) -> bool:
        cp = self._simctl(["get_app_container", self.udid, watch_bundle_id(bundle_id)], check=False, quiet=True)
        return cp.returncode == 0

    def launch(self, bundle_id: str, *, terminate_first: bool = True) -> None:
        bid = watch_bundle_id(bundle_id)
        APP_FILE.parent.mkdir(parents=True, exist_ok=True)
        APP_FILE.write_text(bid)
        if is_running():
            call("launch" if terminate_first else "activate", bundleId=bid)
            return
        if terminate_first:
            self._simctl(["terminate", self.udid, bid], check=False, quiet=True)
        self._simctl(["launch", self.udid, bid])

    def terminate(self, bundle_id: str) -> None:
        bid = watch_bundle_id(bundle_id)
        if is_running():
            call("terminate", bundleId=bid)
        else:
            self._simctl(["terminate", self.udid, bid], check=False)

    def app_data_container(self, bundle_id: str) -> Path:
        return xcode.app_container(self.udid, watch_bundle_id(bundle_id), "data")

    # ---- screen (screenshot pixels; the agent works in points) ----
    def screenshot(self, path: Path) -> Path:
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
        s = self.scale
        return [UINode(label=n.get("label") or "", value=n.get("value") or "", identifier=n.get("identifier") or "",
                       role=n.get("role") or "", enabled=bool(n.get("enabled", True)),
                       x=int(n["x"] * s), y=int(n["y"] * s), w=int(n["w"] * s), h=int(n["h"] * s))
                for n in call("tree")]
