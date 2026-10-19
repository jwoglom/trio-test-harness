"""WebDriverAgent (appium/WebDriverAgent) client + runner management.

WDA is an XCUITest bundle that exposes taps, typing, screenshots and the
accessibility tree over HTTP. We build it once with xcodebuild
(`th wda build`) and keep it running on the simulator with
`xcodebuild test-without-building` (`th wda start`). Coordinates in this module
are *points*; SimulatorDriver converts from screenshot pixels using /wda/screen.
"""
from __future__ import annotations

import base64
import glob
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import requests

from .config import CONFIG, REPO_ROOT
from .util import HarnessError, log, port_open, run, tmp_path, wait_for
from . import xcode

WDA_SRC = REPO_ROOT / "vendor" / "WebDriverAgent"
WDA_DD = REPO_ROOT / "build" / "wda-dd"
PID_FILE = REPO_ROOT / "runs" / ".tmp" / "wda.pid"
UDID_FILE = REPO_ROOT / "runs" / ".tmp" / "wda-udid.txt"     # the simulator the runner is on
LOG_FILE = REPO_ROOT / "runs" / ".tmp" / "wda.log"
PORT = int(CONFIG.get("TH_WDA_PORT", "8100"))


def ensure_source() -> Path:
    if not (WDA_SRC / "WebDriverAgent.xcodeproj").exists():
        WDA_SRC.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--depth", "1", "https://github.com/appium/WebDriverAgent.git", str(WDA_SRC)], timeout=900)
    return WDA_SRC


def xctestrun() -> Path | None:
    hits = sorted(glob.glob(str(WDA_DD / "Build" / "Products" / "*iphonesimulator*.xctestrun")))
    return Path(hits[-1]) if hits else None


def from_prebuilt() -> Path | None:
    """Use a WDA built ahead of time (TH_WDA_PREBUILT: a derivedData dir with Build/Products/*.xctestrun).

    The products are copied (an APFS clone, so instant) because test-without-building writes into its
    derivedData; the prebuilt tree stays read-only and shared between runs.
    """
    pre = CONFIG.get("TH_WDA_PREBUILT")
    if not pre or not glob.glob(str(Path(pre) / "Build" / "Products" / "*iphonesimulator*.xctestrun")):
        return None
    (WDA_DD / "Build").mkdir(parents=True, exist_ok=True)
    src = str(Path(pre) / "Build" / "Products")
    if subprocess.run(["cp", "-cR", src, str(WDA_DD / "Build")], stderr=subprocess.DEVNULL).returncode != 0:
        run(["cp", "-R", src, str(WDA_DD / "Build")])
    log(f"using prebuilt WebDriverAgent from {pre}")
    return xctestrun()


def build(udid: str) -> Path:
    ensure_source()
    log("building WebDriverAgentRunner for the simulator (one-off, several minutes)")
    run(["xcodebuild", "build-for-testing", "-project", str(WDA_SRC / "WebDriverAgent.xcodeproj"),
         "-scheme", "WebDriverAgentRunner", "-destination", f"id={udid}", "-derivedDataPath", str(WDA_DD),
         "CODE_SIGNING_ALLOWED=NO", "COMPILER_INDEX_STORE_ENABLE=NO", "-quiet"],
        env=xcode.developer_env(), timeout=3600, capture=False)
    p = xctestrun()
    if not p:
        raise HarnessError("WDA build produced no .xctestrun")
    return p


def is_running() -> bool:
    if not port_open("127.0.0.1", PORT, 0.5):
        return False
    try:
        return requests.get(f"http://127.0.0.1:{PORT}/status", timeout=3).json()["value"]["ready"]
    except Exception:  # noqa: BLE001
        return False


def start(udid: str, timeout: float = 600) -> None:
    if is_running():
        if not UDID_FILE.exists() or UDID_FILE.read_text().strip() == udid:
            return
        log("WebDriverAgent runs on another simulator; moving it")
        stop()
    xr = xctestrun() or from_prebuilt() or build(udid)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["xcodebuild", "test-without-building", "-xctestrun", str(xr), "-destination", f"id={udid}",
           "-derivedDataPath", str(WDA_DD)]
    log(f"starting WDA runner on simulator {udid} (log: {LOG_FILE})")
    with open(LOG_FILE, "w") as fh:
        p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, env={**os.environ, **xcode.developer_env()},
                             start_new_session=True)
    PID_FILE.write_text(str(p.pid))
    UDID_FILE.write_text(udid)
    try:
        wait_for(is_running, timeout, 1.0, what="WebDriverAgent /status")
    except HarnessError:
        tail = LOG_FILE.read_text()[-1500:] if LOG_FILE.exists() else ""
        raise HarnessError(f"WDA did not come up on :{PORT}\n{tail}")


def stop() -> None:
    if PID_FILE.exists():
        try:
            os.killpg(int(PID_FILE.read_text()), signal.SIGTERM)
        except (ProcessLookupError, ValueError, PermissionError):
            pass
        PID_FILE.unlink()


class WDAClient:
    def __init__(self, port: int = PORT):
        self.base = f"http://127.0.0.1:{port}"
        self._sid: str | None = None
        self._scale: float | None = None

    def _req(self, method: str, path: str, body: dict | None = None, session: bool = True, timeout: float = 60):
        url = self.base + (f"/session/{self.sid}" if session else "") + path
        r = requests.request(method, url, json=body, timeout=timeout)
        try:
            data = r.json()
        except ValueError:
            raise HarnessError(f"WDA {method} {path}: non-JSON response {r.status_code}")
        if r.status_code >= 400 or (isinstance(data.get("value"), dict) and data["value"].get("error")):
            raise HarnessError(f"WDA {method} {path} failed: {json.dumps(data.get('value'))[:400]}")
        return data.get("value")

    @property
    def sid(self) -> str:
        if self._sid is None:
            st = requests.get(self.base + "/status", timeout=10).json()
            self._sid = st.get("sessionId") or None
            if not self._sid:
                v = self._req("POST", "/session", {"capabilities": {"alwaysMatch": {}}}, session=False)
                self._sid = v["sessionId"]
        return self._sid

    def session(self, bundle_id: str | None = None) -> str:
        caps = {"alwaysMatch": {"bundleId": bundle_id}} if bundle_id else {"alwaysMatch": {}}
        v = self._req("POST", "/session", {"capabilities": caps}, session=False)
        self._sid = v["sessionId"]
        return self._sid

    @property
    def scale(self) -> float:
        if self._scale is None:
            self._scale = float(self._req("GET", "/wda/screen")["scale"])
        return self._scale

    def screenshot_png(self) -> bytes:
        return base64.b64decode(self._req("GET", "/screenshot", session=False))

    def tap(self, x: float, y: float) -> None:      # points
        try:
            self._req("POST", "/wda/tap", {"x": x, "y": y})
        except HarnessError:
            self._req("POST", "/wda/tap/0", {"x": x, "y": y})

    def touch_and_hold(self, x: float, y: float, duration: float) -> None:
        self._req("POST", "/wda/touchAndHold", {"x": x, "y": y, "duration": duration})

    def drag(self, x1: float, y1: float, x2: float, y2: float, duration: float = 0.3) -> None:
        self._req("POST", "/wda/dragfromtoforduration",
                  {"fromX": x1, "fromY": y1, "toX": x2, "toY": y2, "duration": duration})

    def keys(self, text: str) -> None:
        self._req("POST", "/wda/keys", {"value": list(text)})

    def press_button(self, name: str) -> None:
        self._req("POST", "/wda/pressButton", {"name": name})

    def home(self) -> None:
        self._req("POST", "/wda/homescreen", session=False)

    def lock(self) -> None:
        """Lock the device (on the simulator WDA presses the side button)."""
        self._req("POST", "/wda/lock", session=False)

    def unlock(self) -> None:
        self._req("POST", "/wda/unlock", session=False)

    def is_locked(self) -> bool:
        return bool(self._req("GET", "/wda/locked", session=False))

    def source(self) -> dict:
        return self._req("GET", "/source?format=json", session=False, timeout=120)

    def find_elements(self, text: str, exact: bool = False) -> list[dict]:
        """Predicate search (fast, ~0.5s) -> [{label,name,value,type,enabled,rect(points)}]."""
        esc = text.replace("'", "\\'")
        op = "==" if exact else "CONTAINS[c]"
        pred = f"label {op} '{esc}' OR name {op} '{esc}' OR value {op} '{esc}'"
        els = self._req("POST", "/elements", {"using": "predicate string", "value": pred}, timeout=90)
        out = []
        for e in els[:25]:
            eid = e.get("ELEMENT") or next(iter(e.values()))
            try:
                rect = self._req("GET", f"/element/{eid}/rect")
                attrs = {k: self._req("GET", f"/element/{eid}/attribute/{k}") for k in ("label", "name", "value", "type", "enabled")}
            except HarnessError:
                continue
            out.append({"rect": rect, **attrs})
        return out

    def click(self, name: str) -> bool:
        """Tap the first element named or labelled `name` as an element (XCUITest's own tap), not at a coordinate."""
        esc = name.replace("'", "\\'")
        els = self._req("POST", "/elements", {"using": "predicate string", "value": f"name == '{esc}' OR label == '{esc}'"})
        if not els:
            return False
        eid = els[0].get("ELEMENT") or next(iter(els[0].values()))
        self._req("POST", f"/element/{eid}/click")
        return True

    def alert_text(self) -> str | None:
        try:
            return self._req("GET", "/alert/text")
        except HarnessError:
            return None

    def alert_accept(self) -> None:
        self._req("POST", "/alert/accept")

    def active_bundle(self) -> str | None:
        try:
            return self._req("GET", "/wda/activeAppInfo")["bundleId"]
        except HarnessError:
            return None
