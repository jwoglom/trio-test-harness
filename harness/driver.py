"""Common driver interface shared by the Simulator (idb) and physical iPhone (TrollVNC) backends.

All coordinates are in screenshot pixel space.
"""
from __future__ import annotations

import re
import time
from .util import tmp_path
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class UINode:
    label: str
    value: str
    identifier: str
    role: str
    enabled: bool
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    def matches(self, text: str, *, exact: bool = False) -> bool:
        hay = [self.label, self.value, self.identifier]
        if exact:
            return any(h == text for h in hay)
        t = text.lower()
        return any(t in h.lower() for h in hay if h)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cx"], d["cy"] = self.center
        return d


class Driver:
    kind: str = "abstract"

    # lifecycle
    def boot(self) -> None: ...
    def wake(self) -> None: ...

    # apps
    def install(self, app_path: Path) -> None: raise NotImplementedError
    def launch(self, bundle_id: str, *, terminate_first: bool = True) -> None: raise NotImplementedError
    def terminate(self, bundle_id: str) -> None: raise NotImplementedError

    # screen
    def screenshot(self, path: Path) -> Path: raise NotImplementedError
    def screen_size(self) -> tuple[int, int]: raise NotImplementedError

    # input
    def tap(self, x: int, y: int, *, duration: float = 0.0) -> None: raise NotImplementedError
    def swipe(self, x1: int, y1: int, x2: int, y2: int, *, duration: float = 0.3) -> None: raise NotImplementedError
    def drag(self, x1: int, y1: int, x2: int, y2: int, *, hold: float = 0.8, duration: float = 0.8, settle: float = 0.5) -> None:
        """Long-press at (x1,y1) for `hold` s, move to (x2,y2) over `duration` s, rest `settle` s, release.
        This is what SwiftUI .draggable/.dropDestination and reorderable lists need; `swipe` moves immediately.
        Backends without a native gesture fall back to a slow swipe."""
        self.swipe(x1, y1, x2, y2, duration=hold + duration)
    def type_text(self, text: str) -> None: raise NotImplementedError
    def key(self, name: str) -> None: raise NotImplementedError
    def home(self) -> None: raise NotImplementedError

    # accessibility (may be unavailable -> empty list)
    def ui_tree(self) -> list[UINode]:
        return []

    # ---- helpers built on the primitives ----
    def find(self, text: str, *, exact: bool = False, role: str | None = None) -> list[UINode]:
        nodes = [n for n in self.ui_tree() if n.matches(text, exact=exact)]
        if role:
            nodes = [n for n in nodes if role.lower() in n.role.lower()]
        return nodes

    def tap_text(self, text: str, *, exact: bool = False, index: int = 0, ocr_fallback: bool = True,
                 screenshot_for_ocr: Path | None = None) -> tuple[int, int]:
        """Tap an element by accessibility label/id; fall back to OCR on a screenshot."""
        nodes = self.find(text, exact=exact)
        nodes = [n for n in nodes if n.w > 0 and n.h > 0]
        if nodes:
            x, y = nodes[index if -len(nodes) <= index < len(nodes) else -1].center
            self.tap(x, y)
            return x, y
        if ocr_fallback:
            from .ocr import find_text
            shot = screenshot_for_ocr or tmp_path("ocr.png")
            self.screenshot(shot)
            hits = find_text(shot, text)
            if hits:
                x, y = hits[index if -len(hits) <= index < len(hits) else -1].center
                self.tap(x, y)
                return x, y
        raise LookupError(f"no UI element matching {text!r}")

    def wait_for_text(self, text: str, timeout: float = 20, interval: float = 1.0, *, ocr: bool = True) -> bool:
        deadline = time.time() + timeout
        shot = tmp_path("wait.png")
        while time.time() < deadline:
            if self.find(text):
                return True
            if ocr:
                from .ocr import find_text
                self.screenshot(shot)
                if find_text(shot, text):
                    return True
            time.sleep(interval)
        return False

    def scroll(self, direction: str = "down", amount: float = 0.5) -> None:
        w, h = self.screen_size()
        cx = w // 2
        dy = int(h * amount)
        if direction == "down":      # reveal content below: drag finger up
            self.swipe(cx, int(h * 0.75), cx, int(h * 0.75) - dy)
        elif direction == "up":
            self.swipe(cx, int(h * 0.25), cx, int(h * 0.25) + dy)
        elif direction == "left":
            self.swipe(int(w * 0.85), h // 2, int(w * 0.15), h // 2)
        elif direction == "right":
            self.swipe(int(w * 0.15), h // 2, int(w * 0.85), h // 2)
