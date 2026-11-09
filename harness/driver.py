"""Common driver interface shared by the Simulator (idb) and physical iPhone (TrollVNC) backends.

All coordinates are in screenshot pixel space.
"""
from __future__ import annotations

import re
import time
from .util import tmp_path
from dataclasses import dataclass, asdict
from pathlib import Path

MIN_SCROLL_PIXELS = 50      # an amount from 1 up to this is neither a fraction nor a distance worth a swipe
MAX_SWIPE_FRACTION = 0.7    # of the screen: a scroll's swipe starts at 75% (or 25%) of its height and ends on it


def scroll_distance(amount: float, extent: int) -> int:
    """Pixels to scroll: `amount` is a fraction of `extent` up to 1, else pixels."""
    if 0 < amount <= 1:
        return int(extent * amount)
    if amount >= MIN_SCROLL_PIXELS:
        return int(amount)
    raise ValueError(f"scroll amount {amount:g}: give a fraction of the screen (0.5 is half of it) or pixels "
                     f"({MIN_SCROLL_PIXELS} or more)")


def clamp_to_screen(size: tuple[int, int], x: float, y: float) -> tuple[int, int]:
    """A swipe's point moved onto the screen: WebDriverAgent hung for minutes on one dragged a thousand screens past
    its top. Taps are left alone, since a row just below a sheet's edge takes one (WebDriverAgent scrolls it in)."""
    w, h = size
    return int(min(max(x, 0), w - 1)), int(min(max(y, 0), h - 1))


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
    def multi_tap(self, x: int, y: int, count: int = 2, *, interval: float = 0.1) -> None:
        """`count` taps at one point, `interval` s apart, sent as one gesture. A SwiftUI `onTapGesture(count:)` (or a
        UIKit numberOfTapsRequired) takes only taps that each follow the last within about 0.3 s, which separate `th tap`
        calls, a process and a round-trip each, never manage. Backends override this to send the taps in one go."""
        for i in range(count):
            if i:
                time.sleep(interval)
            self.tap(x, y)
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

    def tap_element(self, text: str) -> tuple[int, int] | None:
        """Tap an element by its exact label or identifier through the backend's own element tap where it has one
        (WebDriverAgent): a view another process draws in the app (Apple Health's access sheet) ignores coordinate
        taps. Elsewhere an ordinary tap_text."""
        return self.tap_text(text, exact=True, ocr_fallback=False)

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
        """`amount`: how far up or down, as a fraction of the screen's height, or in pixels from MIN_SCROLL_PIXELS."""
        w, h = self.screen_size()
        cx = w // 2
        dy = scroll_distance(amount, h)
        # A swipe stays on the screen (WebDriverAgent hung for minutes on one dragged a thousand screens past its
        # top), so a longer scroll is several swipes.
        while direction in ("down", "up") and dy > 0:
            step = min(dy, int(h * MAX_SWIPE_FRACTION))
            if direction == "down":      # reveal content below: drag finger up
                self.swipe(cx, int(h * 0.75), cx, int(h * 0.75) - step)
            else:
                self.swipe(cx, int(h * 0.25), cx, int(h * 0.25) + step)
            dy -= step
            if dy > 0:
                time.sleep(0.6)
        if direction == "left":
            self.swipe(int(w * 0.85), h // 2, int(w * 0.15), h // 2)
        elif direction == "right":
            self.swipe(int(w * 0.15), h // 2, int(w * 0.85), h // 2)
