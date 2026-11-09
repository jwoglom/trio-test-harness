"""TandemKit's own screens, the same in Trio and Loop: its settings screen ▸ View Debug Options ▸ Feature Flags.

Facts (TandemKit, Sources/TandemKitUI/Views/Settings/TandemKitFeatureFlagsView.swift) these flows rely on:
  * The Feature Flags screen hides its list behind the pump image while every flag is at its default ("No feature
    flags changed."). A triple tap on the image (`onTapGesture(count: 3)`) reveals it until the screen is left; with a
    flag off its default the list shows at once.
  * Each flag is a Toggle titled `TandemFeatureFlags.Flag.title`. The flags are read from the build's TandemKit
    (FLAGS_SOURCE), since a PR can add one; FLAGS below stands in when that checkout is not at hand.
  * The app reaches TandemKit's settings screen its own way: `open_pump_settings` in flows/trio.py and flows/loop.py.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from ..driver import Driver, UINode
from ..report import Run
from ..util import HarnessError, log, tmp_path

FLAGS_SOURCE = "Sources/TandemKit/State/TandemFeatureFlags.swift"


@dataclass(frozen=True)
class Flag:
    raw: str                 # TandemFeatureFlags.Flag's rawValue
    title: str               # the Toggle's title
    default: bool | None     # None: not read from the source
    description: str = ""


# Stand-in for FLAGS_SOURCE when the build's checkout is not at hand.
FLAGS = {f.raw: f for f in (
    Flag("controlIQSupport", "Control-IQ Support", False),
    Flag("quickBolusButtonSupport", "Quick Bolus Button Support", True),
    Flag("historyBackfill", "History Backfill", True),
    Flag("historyDoseReporting", "History Dose Reporting", True),
    Flag("basalHistoryReporting", "History Basal Reporting", True),
    Flag("manualTempBasalSupport", "Manual Temp Basal Support", False),
)}
DEBUG_OPTIONS = "View Debug Options"
FEATURE_FLAGS = "Feature Flags"
EMPTY_STATE = "No feature flags changed."
PUMP_IMAGE = "tandem_mobi"


def _swift_property(swift: str, name: str) -> str:
    """The body of `var <name>` in the Flag enum, up to the next declaration."""
    m = re.search(rf"\bvar {name}\b[^{{]*\{{(.*?)(?=\n\s*(?:public |private |internal )?(?:var|func|static|init)\b|\Z)",
                  swift, re.S)
    return m.group(1) if m else ""


def parse_flags(swift: str) -> dict[str, Flag]:
    """TandemFeatureFlags.Flag's cases with their title, default and description, from TandemFeatureFlags.swift."""
    enum = re.search(r"\benum Flag\b[^{]*\{(.*?)\bvar id\b", swift, re.S)
    if not enum:
        return {}
    raws = re.findall(r"^\s*case (\w+)\s*$", enum.group(1), re.M)
    defaults: dict[str, bool] = {}
    for m in re.finditer(r"case ((?:\.\w+[\s,]*)+):\s*return (true|false)", _swift_property(swift, "defaultValue")):
        for raw in re.findall(r"\.(\w+)", m.group(1)):
            defaults[raw] = m.group(2) == "true"
    localized = r"case \.(\w+):\s*return String\(\s*localized:\s*\"((?:[^\"\\]|\\.)*)\""
    titles = dict(re.findall(localized, _swift_property(swift, "title")))
    descriptions = dict(re.findall(localized, _swift_property(swift, "flagDescription")))
    return {raw: Flag(raw, titles.get(raw, raw), defaults.get(raw), descriptions.get(raw, "")) for raw in raws}


def flags_in_checkout(source_dir: str | Path | None) -> dict[str, Flag]:
    """The flags of the TandemKit in an app checkout (Trio's TandemKit submodule, or one a level down); {} without one."""
    if not source_dir:
        return {}
    src = Path(source_dir).expanduser()
    for path in [src / "TandemKit" / FLAGS_SOURCE, *sorted(src.glob(f"*/TandemKit/{FLAGS_SOURCE}"))]:
        try:
            if flags := parse_flags(path.read_text()):
                return flags
        except OSError:
            continue
    return {}


def known_flags(run: Run | str | Path | None = None) -> dict[str, Flag]:
    """The flags of the run's build (its run.json names the checkout it was built from), else FLAGS."""
    path = run.path if isinstance(run, Run) else run
    if path:
        try:
            build = (json.loads((Path(path) / "run.json").read_text()).get("build") or {})
        except (OSError, ValueError):
            build = {}
        if flags := flags_in_checkout(build.get("source_dir")):
            return flags
    return FLAGS


def flag_title(name: str, flags: dict[str, Flag] | None = None) -> str:
    """A flag's title from its title or rawValue, any case."""
    flags = flags or FLAGS
    for f in flags.values():
        if name.lower() in (f.raw.lower(), f.title.lower()):
            return f.title
    raise HarnessError(f"unknown TandemKit feature flag {name!r} ({' | '.join(flags)})")


def agent_notes(run_dir: str | Path) -> str:
    """ci/agent-tandemkit-flags.md with the build's flags listed: how a job's agent turns one on."""
    flags = known_flags(run_dir)
    lines = []
    for f in flags.values():
        default = "" if f.default is None else f", {'on' if f.default else 'off'} by default"
        lines.append(f"- `{f.raw}`, \"{f.title}\"{default}." + (f" {f.description}" if f.description else ""))
    from ..config import REPO_ROOT
    return (REPO_ROOT / "ci" / "agent-tandemkit-flags.md").read_text().replace("{flags}", "\n".join(lines))


def _step(run: Run | None, drv: Driver, action: str, note: str) -> Path | None:
    from .trio import step
    return step(run, drv, action, note)


# Backends whose find() sees the screen (WebDriverAgent's tree; Vision OCR in the mirror). TrollVNC's needs tesseract.
FINDS_TEXT = {"sim", "mirror", "watch"}


def _on_screen(drv: Driver, text: str, shot: Path | None = None) -> tuple[int, int] | None:
    """Centre of `text` on screen, from the accessibility tree or OCR."""
    nodes = [n for n in drv.find(text) if n.w > 0 and n.h > 0]
    if nodes:
        return nodes[0].center
    if drv.kind in FINDS_TEXT:
        return None
    from ..ocr import find_text
    shot = shot or drv.screenshot(tmp_path("tandemkit.png"))
    hits = find_text(shot, text)
    return hits[0].center if hits else None


def _scroll_to(drv: Driver, text: str, *, max_scrolls: int = 6) -> tuple[int, int]:
    """Scroll down until `text` shows."""
    for i in range(max_scrolls + 1):
        pt = _on_screen(drv, text)
        if pt:
            return pt
        if i < max_scrolls:
            drv.scroll("down", 0.4)
            time.sleep(0.8)
    raise LookupError(f"{text!r} not found on TandemKit's screen")


def open_feature_flags(drv: Driver, run: Run | None = None) -> None:
    """From TandemKit's settings screen: View Debug Options ▸ Feature Flags."""
    drv.tap(*_scroll_to(drv, DEBUG_OPTIONS))
    time.sleep(1.5)
    _step(run, drv, "tandemkit-debug-options", "TandemKit ▸ View Debug Options")
    drv.tap(*_scroll_to(drv, FEATURE_FLAGS))
    time.sleep(1.5)
    _step(run, drv, "tandemkit-feature-flags", "TandemKit ▸ Debug Options ▸ Feature Flags")


def flags_listed(drv: Driver) -> bool:
    shot = None if drv.kind in FINDS_TEXT else drv.screenshot(tmp_path("tandemkit-flags.png"))
    return any(_on_screen(drv, FLAGS[raw].title, shot) for raw in ("controlIQSupport", "historyBackfill"))


def _scale(drv: Driver) -> float | None:
    """Pixels per point where the backend knows it (simulator, watch); None on the phone."""
    scale = getattr(drv, "scale", None)
    return float(scale) if isinstance(scale, (int, float)) and scale > 0 else None


def _pump_image_point(drv: Driver) -> tuple[int, int]:
    """The pump image above the empty state's caption: 60 pt above the caption (the image is 96 pt high, 12 pt above
    it), or the caption's line height three times over where the scale is unknown. An image node is taken only when it
    sits there: TandemKit's settings screen, still in the navigation stack below, has a `tandem_mobi` image of its own
    near the top, which WebDriverAgent's search returns first (run 42538914279 triple-tapped empty space there)."""
    caption = [n for n in drv.find(EMPTY_STATE) if n.w > 0 and n.h > 0]
    if caption:
        top, cx, line = caption[0].y, caption[0].center[0], caption[0].h
    else:
        from ..ocr import find_text
        hits = find_text(drv.screenshot(tmp_path("tandemkit-empty.png")), "No feature flags")
        if not hits:
            raise LookupError(f"neither the feature flags nor {EMPTY_STATE!r} is on screen")
        top, cx, line = hits[0].y, hits[0].center[0], hits[0].h
    scale = _scale(drv)
    above = int(60 * scale) if scale else 3 * line
    img = [n for n in drv.find(PUMP_IMAGE) + drv.find(PUMP_IMAGE.replace("_", " ")) if n.w > 0 and n.h > 0
           and abs(n.center[1] - (top - above)) <= above // 2 and abs(n.center[0] - cx) <= above]
    return img[0].center if img else (cx, max(0, top - above))


def reveal_feature_flags(drv: Driver, run: Run | None = None, *, attempts: int = 3) -> None:
    """Triple-tap the pump image until the flag list shows. A no-op when it already does."""
    if flags_listed(drv):
        return
    x, y = _pump_image_point(drv)
    for i in range(attempts):
        log(f"triple tap on the pump image at {x},{y}")
        drv.multi_tap(x, y, 3, interval=0.1 if i < attempts - 1 else 0.05)
        time.sleep(1.0)
        if flags_listed(drv):
            _step(run, drv, "tandemkit-feature-flags-revealed", "Feature Flags after a triple tap on the pump image")
            return
    _step(run, drv, "tandemkit-feature-flags-hidden", "Feature Flags still hidden after the triple taps")
    raise HarnessError(f"the triple tap at {x},{y} did not reveal the feature flags")


def _switch(drv: Driver, title: str) -> UINode | None:
    nodes = [n for n in drv.find(title, exact=True) if n.w > 0 and "switch" in n.role.lower()]
    return nodes[0] if nodes else None


def _switch_point(drv: Driver, title: str) -> tuple[int, int]:
    """The toggle's switch: at the trailing edge of the row its title is on."""
    scale = _scale(drv)
    node = _switch(drv, title)
    if node:
        # A Toggle's element spans its row, and only the 51 pt switch at its trailing end takes a tap.
        return node.x + node.w - (int(26 * scale) if scale else node.h), node.center[1]
    pt = _scroll_to(drv, title)
    w, _ = drv.screen_size()
    return w - (int(61 * scale) if scale else int(w * 0.155)), pt[1]


def _is_on(drv: Driver, title: str, point: tuple[int, int]) -> bool | None:
    """The switch's state: the tree's value, or a tinted (on) versus grey (off) switch in a screenshot."""
    node = _switch(drv, title)
    if node and node.value in ("0", "1"):
        return node.value == "1"
    from PIL import Image
    im = Image.open(drv.screenshot(tmp_path("tandemkit-switch.png"))).convert("RGB")
    x, y = point
    r = max(6, im.width // 60)
    tinted = total = 0
    for px in range(max(0, x - 3 * r), min(im.width, x + 3 * r), 2):
        for py in range(max(0, y - r), min(im.height, y + r), 2):
            c = im.getpixel((px, py))
            total += 1
            tinted += max(c) - min(c) > 80
    return None if not total else tinted > total * 0.2


def set_feature_flag(drv: Driver, run: Run | None, name: str, enabled: bool = True) -> bool:
    """Set a flag on the Feature Flags screen, revealing the list first. Returns whether it changed."""
    title = flag_title(name, known_flags(run))
    reveal_feature_flags(drv, run)
    point = _switch_point(drv, title)
    state = _is_on(drv, title, point)
    if state is enabled:
        log(f"{title} is already {'on' if enabled else 'off'}")
        _step(run, drv, "tandemkit-feature-flag", f"{title} already {'on' if enabled else 'off'}")
        return False
    drv.tap(*point)
    time.sleep(1.0)
    after = _is_on(drv, title, _switch_point(drv, title))
    _step(run, drv, "tandemkit-feature-flag", f"{title} turned {'on' if enabled else 'off'}")
    if after is not None and after is not enabled:
        raise HarnessError(f"{title} did not turn {'on' if enabled else 'off'} (tapped {point[0]},{point[1]})")
    return True
