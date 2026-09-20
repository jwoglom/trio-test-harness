"""MirrorDriver ("iphone-usb" mode): drive a non-jailbroken iPhone through macOS **iPhone Mirroring**.

Built on ShawnPana/phone-harness (MIT, vendored in vendor/phone-harness and pip-installed into .venv):
the mirroring window is captured with `screencapture`, taps/drags/keys are posted as HID-level CGEvents,
text is located with Apple's Vision OCR. Nothing runs on the phone; no jailbreak, no Xcode.

Requirements (user's hands): macOS 15+, iPhone iOS 18+, both signed into the same Apple ID, Bluetooth + Wi-Fi on,
iPhone Mirroring paired once (open the app, follow the prompts on the phone), and the terminal granted
Accessibility + Screen Recording. The phone must be locked for mirroring to run; unlocking it pauses the mirror.

Coordinates: like every other driver, the harness works in *screenshot pixels*. The capture is the Mac window
(Retina, roughly 2x of the window's points, smaller than the phone's native resolution); we map pixel → window point
with the window bounds phone-harness returns alongside each capture.
"""
from __future__ import annotations

import time
from pathlib import Path

from .driver import Driver, UINode
from .util import HarnessError, log, tmp_path


def _mods():
    try:
        from phone_harness import mirror, ocr  # type: ignore
    except ImportError as e:  # noqa: BLE001
        raise HarnessError("phone-harness not installed: pip install -e vendor/phone-harness (pyobjc)") from e
    return mirror, ocr


class MirrorDriver(Driver):
    kind = "mirror"

    def __init__(self):
        self.mirror, self.vision = _mods()
        self._win = None
        self._img_size: tuple[int, int] | None = None

    # ---- window / session ----
    def status(self) -> dict:
        app = self.mirror.running_app()
        win = self.mirror.find_window() if app else None
        return {"app_running": app is not None, "window": win}

    def boot(self) -> None:
        self.mirror.ensure_window()

    def wake(self) -> None:
        self.mirror.activate()

    def _capture(self, path: Path) -> Path:
        p, win = self.mirror.capture(str(path))
        self._win = win
        from PIL import Image
        with Image.open(p) as im:
            self._img_size = (im.width, im.height)
        return Path(p)

    def _to_screen(self, x: int, y: int) -> tuple[float, float]:
        if self._win is None or self._img_size is None:
            self._capture(tmp_path("mirror-geom.png"))
        w, h = self._img_size
        win = self.mirror.find_window() or self._win
        return win["x"] + x * win["w"] / w, win["y"] + y * win["h"] / h

    # ---- screen ----
    def screen_size(self) -> tuple[int, int]:
        if self._img_size is None:
            self._capture(tmp_path("mirror-geom.png"))
        return self._img_size

    def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        return self._capture(path)

    # ---- input (pixel coords) ----
    def tap(self, x: int, y: int, *, duration: float = 0.0) -> None:
        gx, gy = self._to_screen(x, y)
        if duration:
            self.mirror.long_press(gx, gy, duration)
        else:
            self.mirror.tap(gx, gy)
        time.sleep(0.2)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, *, duration: float = 0.3) -> None:
        a = self._to_screen(x1, y1); b = self._to_screen(x2, y2)
        self.mirror.drag(a[0], a[1], b[0], b[1], duration=duration)
        time.sleep(0.3)

    def type_text(self, text: str) -> None:
        self.mirror.type_text(text)

    def key(self, name: str) -> None:
        combo = {"return": "return", "enter": "return", "backspace": "delete", "delete": "delete", "tab": "tab",
                 "escape": "escape", "space": "space"}.get(name.lower(), name)
        self.mirror.press(combo)

    def home(self) -> None:
        self.mirror.press("cmd+1")      # iPhone Mirroring: ⌘1 = Home Screen, ⌘2 = App Switcher, ⌘3 = Spotlight
        time.sleep(0.8)

    def spotlight(self) -> None:
        self.mirror.press("cmd+3")
        time.sleep(0.8)

    def open_app_via_spotlight(self, name: str) -> None:
        self.spotlight()
        self.type_text(name)
        time.sleep(1.2)
        self.key("return")
        time.sleep(2.0)

    def launch(self, bundle_id: str, *, terminate_first: bool = True) -> None:
        raise HarnessError("MirrorDriver.launch: use open_app_via_spotlight('Trio') (no process control without root)")

    def terminate(self, bundle_id: str) -> None:
        raise HarnessError("MirrorDriver cannot kill processes; swipe the app away in the App Switcher (cmd+2)")

    def install(self, app_path: Path) -> None:
        raise HarnessError("install through devicectl (signed build) — iPhone Mirroring cannot install apps")

    # ---- text lookup: Vision OCR (pixel coords) ----
    def ui_tree(self) -> list[UINode]:
        """Vision OCR boxes as UINodes. phone-harness returns box *centers in screen points*; convert to capture pixels."""
        shot = self._capture(tmp_path("mirror-ocr.png"))
        win = self._win
        iw, ih = self._img_size
        sx, sy = iw / win["w"], ih / win["h"]
        nodes = []
        for o in self.vision.recognize(str(shot), win):
            w = int(o["w"] * sx); h = int(o["h"] * sy)
            cx = (o["x"] - win["x"]) * sx; cy = (o["y"] - win["y"]) * sy
            nodes.append(UINode(label=o.get("text", ""), value=f"conf={o.get('confidence', 0)}", identifier="", role="Text",
                                enabled=True, x=int(cx - w / 2), y=int(cy - h / 2), w=w, h=h))
        return nodes
