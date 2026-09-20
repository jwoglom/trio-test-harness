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
        if via_usb is None:
            # A non-local TH_VNC_HOST means "connect over the network"; USB forwarding only makes sense for localhost.
            via_usb = CONFIG.bool("TH_VNC_VIA_USB") and self.host in ("localhost", "127.0.0.1", "")
        self.via_usb = via_usb
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
            from .rfb import RFBClient
            host, port = ("127.0.0.1", self.local_port) if self.via_usb else (self.host, self.port)
            log(f"vnc connect {host}:{port}")
            try:
                self._client = RFBClient(host, port, self.password).connect()
            except (OSError, HarnessError) as e:
                self._client = None
                raise HarnessError(
                    f"could not connect to TrollVNC at {host}:{port}: {e}\n"
                    "Is the iPhone on, TrollVNC enabled (Settings > TrollVNC), reachable at TH_VNC_HOST / over USB, "
                    "and TH_VNC_PASSWORD set if you configured a password?") from e
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
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

    def is_dark(self) -> bool:
        from PIL import ImageStat
        return ImageStat.Stat(self.client.capture().convert("L")).mean[0] < 8

    def unlock(self, passcode: str | None = None) -> bool:
        """Wake, swipe up, type the passcode (TH_DEVICE_PASSCODE), return True if the lock screen is gone."""
        passcode = passcode or CONFIG.get("TH_DEVICE_PASSCODE")
        w, h = self.screen_size()
        if self.is_dark():
            self.power(); time.sleep(1.0)
        self.swipe(w // 2, int(h * 0.92), w // 2, int(h * 0.25), duration=0.25)
        time.sleep(1.2)
        from .ocr import find_text
        shot = self.screenshot(tmp_path("unlock.png"))
        if not find_text(shot, "Enter Passcode"):
            return True
        if not passcode:
            raise HarnessError("phone is locked and TH_DEVICE_PASSCODE is not set (put it in .env)")
        for ch in passcode:
            self.client.key_press(ch); time.sleep(0.12)
        time.sleep(1.5)
        shot = self.screenshot(tmp_path("unlock2.png"))
        return not find_text(shot, "Enter Passcode")

    # ---- screen ----
    def screen_size(self) -> tuple[int, int]:
        c = self.client
        return c.width, c.height

    def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.client.capture(incremental=False).save(str(path))
        return path

    # ---- input ----
    def _press(self, x: int, y: int, button: int, hold: float) -> None:
        c = self.client
        c.pointer(x, y, 0)
        time.sleep(0.03)
        c.pointer(x, y, button)
        time.sleep(hold)
        c.pointer(x, y, 0)

    def tap(self, x: int, y: int, *, duration: float = 0.0) -> None:
        self._press(int(x), int(y), LEFT, duration or 0.08)
        time.sleep(0.2)

    def long_press(self, x: int, y: int, duration: float = 1.0) -> None:
        self._press(int(x), int(y), LEFT, duration)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, *, duration: float = 0.3) -> None:
        c = self.client
        steps = max(10, int(duration / 0.02))
        c.pointer(x1, y1, 0); time.sleep(0.03)
        c.pointer(x1, y1, LEFT); time.sleep(0.05)
        for i in range(1, steps + 1):
            t = i / steps
            c.pointer(int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t), LEFT)
            time.sleep(duration / steps)
        c.pointer(x2, y2, 0)
        time.sleep(0.4)

    def type_text(self, text: str) -> None:
        c = self.client
        for ch in text:
            c.key_press("enter" if ch == "\n" else ch)
            time.sleep(0.05)

    def key(self, name: str) -> None:
        self.client.key_press(name)

    def home(self) -> None:
        w, h = self.screen_size()
        self._press(w // 2, h // 2, RIGHT, 0.1)
        time.sleep(0.8)

    def power(self) -> None:
        w, h = self.screen_size()
        self._press(w // 2, h // 2, MIDDLE, 0.1)

    def paste_text(self, text: str) -> None:
        """Push text to the iOS clipboard (TrollVNC clipboard sync), then long-press + Paste yourself."""
        self.client.cut_text(text)

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
