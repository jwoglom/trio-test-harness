"""SimulatorDriver: drive the iOS Simulator via simctl + idb (facebook/idb).

Coordinates: the harness works in *screenshot pixel* space everywhere so that a
model looking at a PNG can tap what it sees. idb wants logical points, so we
divide by the device scale factor.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import CONFIG
from .driver import Driver, UINode
from .util import HarnessError, log, run, which
from . import xcode


class SimulatorDriver(Driver):
    kind = "sim"

    def __init__(self, udid: str | None = None):
        self.udid = udid or xcode.ensure_simulator()["udid"]
        self._scale: float | None = None
        self._env = xcode.developer_env()
        self._wda = None

    # ---- backend: WebDriverAgent (preferred) or idb ----
    @property
    def wda(self):
        """WDAClient if the runner is up (start it with `th wda start`), else None."""
        if self._wda is None:
            from . import wda as W
            if W.is_running():
                self._wda = W.WDAClient()
        return self._wda

    def ensure_wda(self):
        from . import wda as W
        if not W.is_running():
            W.start(self.udid)
        self._wda = W.WDAClient()
        return self._wda

    # ---- lifecycle ----
    def boot(self) -> None:
        xcode.boot_simulator(self.udid)
        if self.have_idb():
            run(["idb", "connect", self.udid], env=self._env, check=False, quiet=True)

    def have_idb(self) -> bool:
        return bool(which("idb")) and bool(which("idb_companion"))

    def _idb(self, args: list[str], **kw):
        if not self.have_idb():
            raise HarnessError("idb not installed (brew install facebook/fb/idb-companion; pip install fb-idb)")
        return run(["idb", *args, "--udid", self.udid], env=self._env, **kw)

    def _simctl(self, args: list[str], **kw):
        return xcode.xcrun(["simctl", *args], **kw)

    # ---- scale factor (pixels per point) ----
    @property
    def scale(self) -> float:
        if self._scale is None and self.wda:
            self._scale = self.wda.scale
        if self._scale is None:
            try:
                out = self._idb(["describe", "--json"], quiet=True).stdout
                d = json.loads(out)
                self._scale = float(d.get("screen_dimensions", {}).get("density", 0)) or 3.0
            except Exception:
                self._scale = 3.0
        return self._scale

    def screen_size(self) -> tuple[int, int]:
        if self.have_idb():
            try:
                d = json.loads(self._idb(["describe", "--json"], quiet=True).stdout)["screen_dimensions"]
                return int(d["width"]), int(d["height"])  # pixels
            except Exception:  # noqa: BLE001
                pass
        from PIL import Image
        from .util import tmp_path
        p = self.screenshot(tmp_path("sim-size.png"))
        with Image.open(p) as im:
            return im.width, im.height

    # ---- apps ----
    def install(self, app_path: Path) -> None:
        self._simctl(["install", self.udid, str(app_path)])

    def uninstall(self, bundle_id: str) -> None:
        self._simctl(["uninstall", self.udid, bundle_id], check=False)

    def launch(self, bundle_id: str, *, terminate_first: bool = True) -> None:
        if terminate_first:
            self._simctl(["terminate", self.udid, bundle_id], check=False, quiet=True)
        self._simctl(["launch", self.udid, bundle_id])

    def terminate(self, bundle_id: str) -> None:
        self._simctl(["terminate", self.udid, bundle_id], check=False)

    def is_installed(self, bundle_id: str) -> bool:
        cp = self._simctl(["get_app_container", self.udid, bundle_id], check=False, quiet=True)
        return cp.returncode == 0

    def open_url(self, url: str) -> None:
        self._simctl(["openurl", self.udid, url])

    def app_data_container(self, bundle_id: str) -> Path:
        return xcode.app_container(self.udid, bundle_id, "data")

    def defaults_write(self, bundle_id: str, key: str, value: str, vtype: str = "-string") -> None:
        self._simctl(["spawn", self.udid, "defaults", "write", bundle_id, key, vtype, value])

    # ---- screen ----
    def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._simctl(["io", self.udid, "screenshot", "--type=png", str(path)], quiet=True)
        return path

    # ---- input (pixel coords) ----
    def _need_input_backend(self) -> None:
        if self.wda is None and not self.have_idb():
            raise HarnessError("no input backend: run `th wda start` (WebDriverAgent) or install idb_companion")

    def tap(self, x: int, y: int, *, duration: float = 0.0) -> None:
        self._need_input_backend()
        if self.wda:
            if duration:
                self.wda.touch_and_hold(x / self.scale, y / self.scale, duration)
            else:
                self.wda.tap(x / self.scale, y / self.scale)
            return
        args = ["ui", "tap", str(int(x / self.scale)), str(int(y / self.scale))]
        if duration:
            args += ["--duration", str(duration)]
        self._idb(args, quiet=True)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, *, duration: float = 0.3) -> None:
        self._need_input_backend()
        s = self.scale
        if self.wda:
            self.wda.drag(x1 / s, y1 / s, x2 / s, y2 / s, duration)
            return
        self._idb(["ui", "swipe", str(int(x1 / s)), str(int(y1 / s)), str(int(x2 / s)), str(int(y2 / s)),
                   "--duration", str(duration)], quiet=True)

    def type_text(self, text: str) -> None:
        self._need_input_backend()
        if self.wda:
            self.wda.keys(text)
            return
        self._idb(["ui", "text", text], quiet=True)

    def key(self, name: str) -> None:
        # HID usage codes: https://source.android.com/... (idb takes numeric keycodes)
        self._need_input_backend()
        if self.wda:
            chars = {"return": "\n", "enter": "\n", "backspace": "\b", "delete": "\b", "tab": "\t", "space": " "}
            self.wda.keys(chars.get(name.lower(), name))
            return
        codes = {"return": 40, "enter": 40, "backspace": 42, "delete": 42, "tab": 43, "space": 44, "escape": 41}
        self._idb(["ui", "key", str(codes[name.lower()])], quiet=True)

    def home(self) -> None:
        if self.wda:
            self.wda.home()
            return
        self._idb(["ui", "button", "HOME"], quiet=True)

    def wake(self) -> None:
        pass  # simulator never sleeps

    # ---- accessibility tree ----
    def ui_tree(self) -> list[UINode]:
        if self.wda:
            return self._wda_tree()
        if not self.have_idb():
            return []
        out = self._idb(["ui", "describe-all", "--json", "--nested"], quiet=True).stdout
        data = json.loads(out)
        nodes: list[UINode] = []

        def walk(n: dict):
            fr = n.get("frame") or {}
            try:
                x, y, w, h = (float(fr.get("x", 0)), float(fr.get("y", 0)), float(fr.get("width", 0)),
                              float(fr.get("height", 0)))
            except Exception:
                x = y = w = h = 0.0
            s = self.scale
            nodes.append(UINode(
                label=n.get("AXLabel") or "", value=str(n.get("AXValue") or ""), identifier=n.get("AXUniqueId") or "",
                role=(n.get("type") or n.get("role") or ""), enabled=bool(n.get("enabled", True)),
                x=int(x * s), y=int(y * s), w=int(w * s), h=int(h * s)))
            for c in n.get("children", []) or []:
                walk(c)

        if isinstance(data, list):
            for n in data:
                walk(n)
        else:
            walk(data)
        return nodes

    def _wda_tree(self) -> list[UINode]:
        s = self.scale
        nodes: list[UINode] = []

        def walk(n: dict):
            r = n.get("rect") or {}
            nodes.append(UINode(
                label=n.get("label") or "", value=str(n.get("value") or ""), identifier=n.get("name") or "",
                role=n.get("type") or "", enabled=bool(n.get("isEnabled", True)),
                x=int(float(r.get("x", 0)) * s), y=int(float(r.get("y", 0)) * s),
                w=int(float(r.get("width", 0)) * s), h=int(float(r.get("height", 0)) * s)))
            for c in n.get("children") or []:
                walk(c)

        walk(self.wda.source())
        return nodes

    def describe_point(self, x: int, y: int) -> dict:
        s = self.scale
        out = self._idb(["ui", "describe-point", str(int(x / s)), str(int(y / s)), "--json"], quiet=True).stdout
        return json.loads(out)
