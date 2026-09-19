"""VNCDriver: drive a physical iPhone running TrollVNC (https://github.com/owngoal-dev/TrollVNC).

Transport: by default TrollVNC's port (5901) is forwarded over USB with `iproxy`
(libusbmuxd) so nothing depends on Wi-Fi. Set TH_VNC_VIA_USB=0 and TH_VNC_HOST to
connect over the network instead.

TrollVNC input mapping (from its README):
  left button  = finger (hold to drag)     right button = Home button
  middle button = Power button             keyboard    = HID keys
Framebuffer may be scaled by TrollVNC's "Scale" preference; we always work in
framebuffer pixels, which is also what our screenshots contain.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .config import CONFIG
from .driver import Driver
from .util import tmp_path, HarnessError, log, port_open, run, which, wait_for

# vncdotool button masks
LEFT, MIDDLE, RIGHT = 1, 2, 4


class VNCDriver(Driver):
    kind = "device"

    def __init__(self, host: str | None = None, port: int | None = None, password: str | None = None,
                 via_usb: bool | None = None, udid: str | None = None):
        self.host = host or CONFIG.get("TH_VNC_HOST")
        self.port = int(port or CONFIG.get("TH_VNC_PORT"))
        self.local_port = int(CONFIG.get("TH_VNC_LOCAL_PORT"))
        self.password = password if password is not None else (CONFIG.get("TH_VNC_PASSWORD") or None)
        self.via_usb = CONFIG.bool("TH_VNC_VIA_USB") if via_usb is None else via_usb
        self.udid = udid or CONFIG.get("TH_DEVICE_UDID") or None
        self._client = None
        self._iproxy: subprocess.Popen | None = None

    # ---- transport ----
    def _ensure_iproxy(self) -> None:
        if not self.via_usb:
            return
        if port_open("127.0.0.1", self.local_port):
            return
        if not which("iproxy"):
            raise HarnessError("iproxy missing (brew install libusbmuxd)")
        cmd = ["iproxy", f"{self.local_port}:{self.port}"]
        if self.udid:
            cmd += ["-u", self.udid]
        log(f"$ {' '.join(cmd)} (background)")
        self._iproxy = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for(lambda: port_open("127.0.0.1", self.local_port), 10, what="iproxy listener")

    def _addr(self) -> str:
        if self.via_usb:
            return f"127.0.0.1::{self.local_port}"
        return f"{self.host}::{self.port}"

    @property
    def client(self):
        if self._client is None:
            self._ensure_iproxy()
            from vncdotool import api
            api.VNCDoToolFactory.protocol.MAX_DESKTOP_SIZE = 0x100000  # allow big framebuffers
            addr = self._addr()
            log(f"vnc connect {addr}")
            try:
                self._client = api.connect(addr, password=self.password, timeout=15)
                self._client.timeout = 30
                # First framebuffer fetch; validates the connection.
                self._client.refreshScreen(incremental=False)
            except Exception as e:  # noqa: BLE001
                self._client = None
                raise HarnessError(
                    f"could not connect to TrollVNC at {addr}: {e}\n"
                    "Is the iPhone plugged in and unlocked, TrollVNC enabled (Settings > TrollVNC), "
                    "and TH_VNC_PASSWORD set if you configured a password?") from e
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        if self._iproxy:
            self._iproxy.terminate()
            self._iproxy = None

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- lifecycle ----
    def boot(self) -> None:
        self.client  # connect

    def wake(self) -> None:
        """Press power once if the screen looks black (device asleep), then swipe up."""
        shot = tmp_path("wake.png")
        self.screenshot(shot)
        from PIL import Image, ImageStat
        im = Image.open(shot).convert("L")
        if ImageStat.Stat(im).mean[0] < 8:
            self.power()
            time.sleep(1.0)
            w, h = self.screen_size()
            self.swipe(w // 2, int(h * 0.9), w // 2, int(h * 0.3), duration=0.3)
            time.sleep(1.0)

    # ---- screen ----
    def screen_size(self) -> tuple[int, int]:
        c = self.client
        c.refreshScreen(incremental=True)
        return c.screen.size

    def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        c = self.client
        c.refreshScreen(incremental=True)
        # Two incremental refreshes coalesce TrollVNC's deferred updates.
        time.sleep(0.15)
        c.refreshScreen(incremental=True)
        c.screen.save(str(path))
        return path

    # ---- input ----
    def _press(self, x: int, y: int, button: int, hold: float) -> None:
        c = self.client
        c.mouseMove(x, y)
        c.mouseDown(button)
        time.sleep(hold)
        c.mouseUp(button)

    def tap(self, x: int, y: int, *, duration: float = 0.0) -> None:
        self._press(int(x), int(y), LEFT, duration or 0.06)
        time.sleep(0.15)

    def long_press(self, x: int, y: int, duration: float = 1.0) -> None:
        self._press(int(x), int(y), LEFT, duration)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, *, duration: float = 0.3) -> None:
        c = self.client
        steps = max(8, int(duration / 0.02))
        c.mouseMove(int(x1), int(y1))
        c.mouseDown(LEFT)
        for i in range(1, steps + 1):
            t = i / steps
            c.mouseMove(int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t))
            time.sleep(duration / steps)
        c.mouseUp(LEFT)
        time.sleep(0.3)

    def type_text(self, text: str) -> None:
        c = self.client
        for ch in text:
            if ch == "\n":
                c.keyPress("enter")
            elif ch == " ":
                c.keyPress("space")
            else:
                c.keyPress(ch)
            time.sleep(0.04)

    def key(self, name: str) -> None:
        aliases = {"return": "enter", "backspace": "bsp", "delete": "del", "escape": "esc"}
        self.client.keyPress(aliases.get(name.lower(), name))

    def home(self) -> None:
        w, h = self.screen_size()
        self._press(w // 2, h // 2, RIGHT, 0.08)
        time.sleep(0.6)

    def power(self) -> None:
        w, h = self.screen_size()
        self._press(w // 2, h // 2, MIDDLE, 0.08)

    def paste_text(self, text: str) -> None:
        """Push text to the iOS clipboard (TrollVNC clipboard sync), then long-press + Paste yourself."""
        self.client.paste(text)

    # ---- app helpers (VNC-only: no process control on the device) ----
    def open_app_via_spotlight(self, name: str) -> None:
        """Home -> swipe down for Spotlight -> type the app name -> tap the first result."""
        self.home()
        w, h = self.screen_size()
        self.swipe(w // 2, int(h * 0.4), w // 2, int(h * 0.7), duration=0.25)
        time.sleep(1.0)
        self.type_text(name)
        time.sleep(1.2)
        self.key("enter")   # Spotlight opens the top hit on return
        time.sleep(2.0)

    def launch(self, bundle_id: str, *, terminate_first: bool = True) -> None:
        # bundle_id is not usable over VNC; callers pass the display name via open_app_via_spotlight.
        raise HarnessError("VNCDriver.launch: use open_app_via_spotlight('Trio') or devicectl (needs Xcode+signing)")

    def terminate(self, bundle_id: str) -> None:
        raise HarnessError("VNCDriver cannot terminate processes; swipe the app away in the App Switcher")

    def install(self, app_path: Path) -> None:
        raise HarnessError("use `th device install-trollstore <ipa>` or devicectl for signed builds")
