"""Trio-specific UI flows. These are heuristics layered on the generic driver;
a human/agent driving interactively should prefer `th shot` + `th tap-text`
when a flow does not fit the screen it sees.

Facts (Trio dev, v0.8.4.x) these flows rely on:
  * Onboarding is gated by Documents/onboardingCompleted.plist (root Bool) in the app
    container. On the simulator we can pre-seed it; on a phone we must click through.
  * Onboarding steps: welcome -> startup info -> overview -> Nightscout (choose
    "Skip Nightscout Setup") -> units & pump -> targets -> basal -> CR -> ISF ->
    delivery limits -> algorithm -> autosens -> SMB -> target behavior ->
    notifications (iOS prompt) -> Bluetooth (in-app alert) -> "Get Started".
  * Home has a tab bar: Main, History, (+ Add Treatment), Adjustments, Settings.
  * Settings -> Devices -> CGM -> Add CGM -> section "Simulator" -> "Glucose Simulator"
    (accessibility id devicePickerItem-simulator).
  * Settings -> Devices -> Insulin Pump -> Add Pump -> "Pump Simulator"
    (accessibility id devicePickerItem-MockPumpManager).
"""
from __future__ import annotations

import plistlib
import time
from pathlib import Path

from ..driver import Driver
from ..report import Run
from ..util import tmp_path, HarnessError, log

NEXT_LABELS = ["Get Started", "Skip Nightscout Setup", "Next", "Continue", "Done", "OK"]
PERMISSION_LABELS = ["Allow", "Allow While Using App", "OK", "Don't Allow"]
HOME_MARKERS = ["Adjustments", "History", "Add Treatment", "Settings"]


def step(run: Run | None, drv: Driver, action: str, note: str = "", *, pause: float = 0.0) -> Path | None:
    """Take a screenshot into the run and log a step. Returns the screenshot path."""
    if pause:
        time.sleep(pause)
    if run is None:
        return None
    p = run.shot_path(action)
    drv.screenshot(p)
    run.log_step(action, note, p)
    log(f"step: {action} — {note}")
    return p


# ---------------- onboarding -----------------

def preseed_onboarding(container: Path) -> list[Path]:
    """Mark onboarding complete in an app data container (simulator only)."""
    docs = container / "Documents"
    docs.mkdir(parents=True, exist_ok=True)
    written = []
    for key in ("onboardingCompleted", "hasSeenFatProteinOrderChange"):
        p = docs / f"{key}.plist"
        p.write_bytes(plistlib.dumps(True, fmt=plistlib.FMT_BINARY))
        written.append(p)
    return written


def _visible(drv: Driver, labels: list[str], shot: Path | None = None) -> str | None:
    nodes = drv.ui_tree()
    if nodes:
        for lab in labels:
            if any(n.matches(lab, exact=True) and n.w > 0 for n in nodes):
                return lab
        return None
    # OCR route (VNC)
    from ..ocr import find_text
    shot = shot or tmp_path("flow.png")
    drv.screenshot(shot)
    for lab in labels:
        if find_text(shot, lab):
            return lab
    return None


def at_home(drv: Driver) -> bool:
    found = 0
    nodes = drv.ui_tree()
    if nodes:
        for m in HOME_MARKERS:
            if any(n.matches(m, exact=True) for n in nodes):
                found += 1
        return found >= 2
    from ..ocr import find_text
    shot = tmp_path("home.png")
    drv.screenshot(shot)
    return sum(1 for m in HOME_MARKERS if find_text(shot, m)) >= 2


def walk_onboarding(drv: Driver, run: Run | None = None, *, max_steps: int = 70, settle: float = 1.2) -> None:
    """Click through onboarding with defaults until the home screen appears."""
    for i in range(max_steps):
        time.sleep(settle)
        if at_home(drv):
            step(run, drv, "home", "Reached the home screen")
            return
        label = _visible(drv, ["Skip Nightscout Setup"]) or _visible(drv, PERMISSION_LABELS[:2]) or _visible(drv, NEXT_LABELS)
        if label is None:
            step(run, drv, f"onboarding-{i:02d}-stuck", "No Next/Continue/Allow button found")
            raise HarnessError("onboarding: no actionable button visible; drive manually with th shot / th tap-text")
        step(run, drv, f"onboarding-{i:02d}", f"tap '{label}'")
        drv.tap_text(label, exact=True)
        if label == "Skip Nightscout Setup":
            time.sleep(settle)
            try:
                drv.tap_text("Next", exact=True)
            except LookupError:
                pass
    raise HarnessError("onboarding: exceeded max steps")


def dismiss_dialogs(drv: Driver, labels: list[str] | None = None, rounds: int = 3) -> int:
    """Tap through blocking alerts (dev-branch warning, version checker, permissions)."""
    n = 0
    for _ in range(rounds):
        lab = _visible(drv, labels or ["Allow", "OK", "Continue", "Dismiss", "Close", "Got it"])
        if not lab:
            break
        drv.tap_text(lab, exact=True)
        n += 1
        time.sleep(0.8)
    return n


# ---------------- settings navigation -----------------

def open_settings(drv: Driver) -> None:
    """Select the Settings tab (last tab). Prefer the tab-bar node when several 'Settings' exist."""
    nodes = [n for n in drv.find("Settings", exact=True) if n.w > 0]
    if nodes:
        w, h = drv.screen_size()
        tab = [n for n in nodes if n.y > h * 0.8] or nodes   # tab bar lives at the bottom
        drv.tap(*tab[-1].center)
    else:
        drv.tap_text("Settings", exact=True, index=-1)
    time.sleep(1.0)


def _tap_seq(drv: Driver, run: Run | None, labels: list[str], settle: float = 1.2) -> None:
    for lab in labels:
        drv.tap_text(lab)
        time.sleep(settle)
        step(run, drv, lab.lower().replace(" ", "-"), f"after tapping '{lab}'")


def add_cgm_simulator(drv: Driver, run: Run | None = None) -> None:
    open_settings(drv)
    step(run, drv, "settings", "Settings root")
    _tap_seq(drv, run, ["Devices", "CGM"])
    dismiss_dialogs(drv, ["Allow", "OK"])
    _tap_seq(drv, run, ["Add CGM", "Glucose Simulator"])
    dismiss_dialogs(drv, ["Continue", "Done", "OK", "Save"])
    step(run, drv, "cgm-simulator-added", "CGM screen after adding Glucose Simulator")


def add_pump_simulator(drv: Driver, run: Run | None = None) -> None:
    open_settings(drv)
    _tap_seq(drv, run, ["Devices", "Insulin Pump", "Add Pump", "Pump Simulator"])
    # MockPumpManager setup: keep defaults and continue until it is dismissed
    for _ in range(6):
        lab = _visible(drv, ["Continue", "Done", "Save", "OK"])
        if not lab:
            break
        drv.tap_text(lab, exact=True)
        time.sleep(1.0)
    step(run, drv, "pump-simulator-added", "Insulin Pump screen after adding Pump Simulator")


def back_to_home(drv: Driver, hops: int = 4) -> None:
    """Pop navigation until the tab bar 'Main' is visible, then select it."""
    for _ in range(hops):
        if at_home(drv):
            break
        try:
            drv.tap_text("Back", exact=True)
        except LookupError:
            w, h = drv.screen_size()
            drv.tap(int(w * 0.06), int(h * 0.075))  # top-left back chevron
        time.sleep(0.8)
    try:
        drv.tap_text("Main", exact=True)
    except LookupError:
        pass
