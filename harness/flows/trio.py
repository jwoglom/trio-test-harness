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
  * Home has a tab bar: Main, History, (+ Add Treatment), Adjustments, Settings. With no devices configured the
    home header shows "Add pump" / "Add CGM" shortcuts (icons at roughly x=12%/50%, y=15% of the screen) that open the
    same pickers as Settings > Devices. After launch a version-check alert "Update Available" needs OK.
  * Picker sheets are dismissed by a swipe-down, so do NOT `scroll` inside them; `tap_text` (WDA) scrolls to the row.
  * The Pump Simulator setup ends on a status page with a "Done" button; the Glucose Simulator needs no setup.
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

# The flows API every app's module has (harness/apps.py picks the module): these names, step, settle, dismiss_dialogs,
# at_home, walk_onboarding, reset_after_install, preseed_simulator, preseed_phone, offers, devices_configured,
# add_cgm_simulator, add_pump_simulator, give_cgm_glucose, open_settings, reset_to_home, back_to_home.
NAME = "Trio"
CGM_SIMULATOR = "Glucose Simulator"
PUMP_SIMULATOR = "Pump Simulator"
ADD_CGM = "Add CGM"
ADD_PUMP = "Add pump"
PROMPTS_AFTER_PRESEED = False      # a pre-seeded Trio opens on its home screen

NEXT_LABELS = ["Get Started", "Skip Nightscout Setup", "Next", "Continue", "Done", "OK"]
PERMISSION_LABELS = ["Allow", "Allow While Using App", "OK", "Don't Allow"]
HOME_MARKERS = ["Adjustments", "History", "Add Treatment", "Settings"]
# Buttons that close interstitials Trio/iOS may throw up at any time (version check, permissions, notices).
# Deliberately excludes Cancel/Close/Back so we never dismiss a sheet the test itself opened.
INTERSTITIAL_BUTTONS = ["OK", "Allow", "Allow While Using App", "Continue", "Not Now", "Later", "Dismiss", "Got it", "Got It"]
# Alert titles worth logging when seen
INTERSTITIAL_TITLES = ["Update Available", "Would Like to", "Bluetooth Required", "Low Glucose", "High Glucose"]


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


def reset_after_install(drv: Driver, bundle_id: str) -> None:
    """Trio's state is in its data container, which a new install starts empty."""


def preseed_simulator(drv: Driver, bundle_id: str) -> list[Path]:
    return preseed_onboarding(drv.app_data_container(bundle_id))


def preseed_phone(bundle_id: str, *, onboarding: bool = True) -> bool:
    """The same flags (and telemetry off) on the jailbroken phone, over SSH."""
    from .. import devices as D
    flags = {"crashlyticsSharingEnabled": False, "telemetrySharingEnabled": False}
    if onboarding:
        flags.update({"onboardingCompleted": True, "hasSeenFatProteinOrderChange": True})
    D.ssh_preseed_flags(bundle_id, flags)
    return True


def give_cgm_glucose(drv: Driver, bundle_id: str) -> bool:
    """Trio's Glucose Simulator has readings from the start."""
    return False


def offers(drv: Driver, label: str) -> bool:
    return bool(drv.find(label, exact=True))


def _visible(drv: Driver, labels: list[str], shot: Path | None = None) -> str | None:
    """First label visible on screen. With an accessibility tree: exact node match. Otherwise OCR on `shot`
    (one screenshot, cached per pass) — button labels only count when they are the whole OCR phrase."""
    nodes = drv.ui_tree()
    if nodes:
        for lab in labels:
            if any(n.matches(lab, exact=True) and n.w > 0 for n in nodes):
                return lab
        return None
    shot = shot or drv.screenshot(tmp_path("flow.png"))
    for lab in labels:
        if _ocr_hits(drv, shot, lab, buttons_only=(lab in NEXT_LABELS)):
            return lab
    return None


BUTTON_ZONE = 0.78   # onboarding/nav buttons live below this fraction of the screen height


def _ocr_hits(drv: Driver, shot: Path, label: str, *, buttons_only: bool = False):
    """OCR hits whose phrase is exactly the label (case-sensitive first letter, so body text like
    'In the next few steps' does not match the 'Next' button). buttons_only keeps the bottom button zone."""
    from ..ocr import find_text
    from PIL import Image
    hits = [h for h in find_text(shot, label, isolated=True)
            if len(h.text.split()) == len(label.split()) and h.text[:1] == label[:1]]
    if buttons_only:
        h_img = Image.open(shot).height
        hits = [h for h in hits if h.y > h_img * BUTTON_ZONE]
    return hits


def _tap_label(drv: Driver, label: str, shot: Path | None) -> None:
    """Tap a label; on OCR targets prefer the lowest hit on screen (buttons live at the bottom, body text above)."""
    if drv.ui_tree() or shot is None:
        drv.tap_text(label, exact=True)
        return
    hits = _ocr_hits(drv, shot, label, buttons_only=(label in NEXT_LABELS))
    if not hits:
        drv.tap_text(label, exact=True)
        return
    x, y = max(hits, key=lambda h: h.y).center
    drv.tap(x, y)


def at_home(drv: Driver, shot: Path | None = None) -> bool:
    found = 0
    nodes = drv.ui_tree()
    if nodes:
        for m in HOME_MARKERS:
            if any(n.matches(m, exact=True) for n in nodes):
                found += 1
        return found >= 2
    shot = shot or drv.screenshot(tmp_path("home.png"))
    return sum(1 for m in HOME_MARKERS if _ocr_hits(drv, shot, m)) >= 2


def walk_onboarding(drv: Driver, run: Run | None = None, *, max_steps: int = 70, settle: float = 1.0) -> None:
    """Click through onboarding with defaults until the home screen appears.

    One screenshot per step (OCR results are cached per image). Order: skip Nightscout, then Next/Continue/Get Started,
    and only if no such button is visible, permission buttons (Allow) — otherwise body text like "You must allow…" wins.
    """
    for i in range(max_steps):
        time.sleep(settle)
        shot = run.shot_path(f"onboarding-{i:02d}") if run else tmp_path("flow.png")
        drv.screenshot(shot)
        if at_home(drv, shot):
            if run:
                run.log_step("home", "Reached the home screen", shot)
            log("step: home")
            return
        # iOS permission alerts sit on top of the page: their exact-word "Allow" wins over the dimmed Next below.
        label = (_visible(drv, PERMISSION_LABELS[:2], shot) or _visible(drv, ["Skip Nightscout Setup"], shot)
                 or _visible(drv, NEXT_LABELS, shot))
        if label is None:
            if run:
                run.log_step(f"onboarding-{i:02d}-stuck", "No Next/Continue/Allow button found", shot)
            raise HarnessError("onboarding: no actionable button visible; drive manually with th shot / th tap-text")
        if run:
            run.log_step(f"onboarding-{i:02d}", f"tap '{label}'", shot)
        log(f"step: onboarding-{i:02d} tap '{label}'")
        _tap_label(drv, label, shot)
        if label == "Skip Nightscout Setup":
            time.sleep(settle)
            try:
                drv.tap_text("Next", exact=True)
            except LookupError:
                pass
    raise HarnessError("onboarding: exceeded max steps")


def _alert_button(drv: Driver, shot: Path, labels: list[str]):
    """An interstitial button on screen: exact isolated word, in the middle band (alerts) — not the tab bar / nav."""
    from PIL import Image
    h_img = Image.open(shot).height
    nodes = drv.ui_tree()
    if nodes:
        for lab in labels:
            hits = [n for n in nodes if n.matches(lab, exact=True) and n.w > 0 and h_img * 0.15 < n.center[1] < h_img * 0.9]
            if hits:
                return lab, hits[0].center
        return None
    for lab in labels:
        hits = [h for h in _ocr_hits(drv, shot, lab) if h_img * 0.15 < h.center[1] < h_img * 0.9]
        if hits:
            return lab, hits[0].center
    return None


def settle(drv: Driver, run: Run | None = None, *, rounds: int = 4, labels: list[str] | None = None) -> int:
    """Clear whatever interstitial is showing (update alert, permission prompt, notice) until the screen is stable.

    Each round: screenshot → find an alert button → tap it → screenshot again; stop when nothing is found or the
    screen did not change. Returns the number of dismissals. Safe to call before any interaction.
    """
    import hashlib
    labels = labels or INTERSTITIAL_BUTTONS
    n = 0
    prev_hash = None
    for i in range(rounds):
        shot = drv.screenshot(tmp_path(f"settle-{i}.png"))
        digest = hashlib.md5(shot.read_bytes()).hexdigest()
        found = _alert_button(drv, shot, labels)
        if not found:
            return n
        if digest == prev_hash:
            log(f"settle: tapping {found[0]!r} did not change the screen; giving up")
            return n
        lab, (x, y) = found
        titles = [t for t in INTERSTITIAL_TITLES if _ocr_hits(drv, shot, t) or drv.find(t)] if not drv.ui_tree() else \
                 [t for t in INTERSTITIAL_TITLES if drv.find(t)]
        if run:
            run.log_step(f"interstitial-{i}", f"dismissed with '{lab}'" + (f" ({', '.join(titles)})" if titles else ""), shot)
        log(f"settle: '{lab}' at {x},{y}" + (f" [{', '.join(titles)}]" if titles else ""))
        drv.tap(x, y)
        n += 1
        prev_hash = digest
        time.sleep(1.0)
    return n


def dismiss_dialogs(drv: Driver, labels: list[str] | None = None, rounds: int = 3) -> int:
    """Backwards-compatible alias of settle()."""
    return settle(drv, rounds=rounds, labels=labels)


# ---------------- settings navigation -----------------

def open_settings(drv: Driver) -> None:
    """Select the Settings tab (last tab). Prefer the tab-bar node when several 'Settings' exist."""
    settle(drv)
    nodes = [n for n in drv.find("Settings", exact=True) if n.w > 0]
    if nodes:
        w, h = drv.screen_size()
        tab = [n for n in nodes if n.y > h * 0.8] or nodes   # tab bar lives at the bottom
        drv.tap(*tab[-1].center)
    else:
        drv.tap_text("Settings", exact=True, index=-1)
    time.sleep(1.0)


def _tap_seq(drv: Driver, run: Run | None, labels: list[str], pause: float = 1.2) -> None:
    """Tap labels in order; before each tap clear interstitials, and retry once if the label is not found."""
    for lab in labels:
        settle(drv, run)
        try:
            drv.tap_text(lab)
        except LookupError:
            time.sleep(1.0); settle(drv, run)
            drv.tap_text(lab)
        time.sleep(pause)
        step(run, drv, lab.lower().replace(" ", "-"), f"after tapping '{lab}'")


def _open_picker_from_home(drv: Driver, run: Run | None, label: str, x_frac: float) -> bool:
    """Use the home-screen 'Add CGM' / 'Add pump' shortcut (icon above the label). Returns False if absent."""
    if not drv.find(label, exact=True):
        return False
    w, h = drv.screen_size()
    drv.tap(int(w * x_frac), int(h * 0.155))
    time.sleep(2.5)
    # The home header stays in the accessibility tree behind a sheet, so finding the label above does
    # not prove the tap opened the picker; the picker's Cancel button does. The sheet took over 7 s on a loaded Mac.
    if not drv.wait_for_text("Cancel", timeout=20, ocr=False):
        step(run, drv, label.lower().replace(" ", "-") + "-no-picker", f"'{label}' shortcut did not open a picker")
        return False
    step(run, drv, label.lower().replace(" ", "-") + "-picker", f"picker opened from the home '{label}' shortcut")
    return True


def _close_device_sheet(drv: Driver) -> None:
    """Newer Trio opens the device's own settings sheet (Close button) right after adding it."""
    if drv.find("Close", exact=True):
        drv.tap_text("Close", exact=True, ocr_fallback=False)
        time.sleep(1.5)


def devices_configured(drv: Driver) -> bool:
    """True when the home header no longer offers Add CGM / Add pump (both devices already set up)."""
    return not drv.find("Add CGM", exact=True) and not drv.find("Add pump", exact=True) \
        and bool(drv.find("Main", exact=True) or drv.find("Settings", exact=True))


def _tap_picker_row(drv: Driver, label: str, max_scrolls: int = 6) -> None:
    """Tap a row in a device picker sheet, scrolling down until SwiftUI has created it.

    The grouped pickers (manufacturer sections, simulators last) are lazy lists: rows far below the
    fold are not in the accessibility tree yet, so looking them up without scrolling fails. Scrolling
    *down* only moves the list; swiping the other way could dismiss the sheet.
    """
    for _ in range(max_scrolls):
        try:
            drv.tap_text(label, exact=True, ocr_fallback=False)
            return
        except LookupError:
            drv.scroll("down", 0.4)
            time.sleep(0.8)
    drv.tap_text(label, exact=True)  # last try, with the OCR fallback


SHEET_BUTTONS = ["Close", "Cancel", "Done"]


def _sheet_open(drv: Driver) -> str | None:
    """A sheet's dismiss button, if one is showing. The home screen behind a sheet stays in the
    accessibility tree, so home markers alone cannot tell "home" from "a sheet over home"."""
    return _visible(drv, SHEET_BUTTONS) if drv.ui_tree() else None


def reset_to_home(drv: Driver, run: Run | None = None, attempts: int = 6) -> bool:
    """Get back to the home screen from wherever a failed step left the app. Returns True when there."""
    for _ in range(attempts):
        settle(drv, run)
        lab = _sheet_open(drv)
        if lab:
            try:
                drv.tap_text(lab, exact=True, ocr_fallback=False)
            except LookupError:
                pass
            time.sleep(1.2)
            continue
        if at_home(drv):
            return True
        back_to_home(drv, hops=2)
        time.sleep(0.8)
    return at_home(drv) and not _sheet_open(drv)


def _diagnose(drv: Driver, run: Run | None, action: str, why: str) -> None:
    """Record what was on screen when a step failed: a screenshot plus the visible UI labels."""
    step(run, drv, action, why)
    if run is None:
        return
    try:
        labels = sorted({n.label for n in drv.ui_tree() if n.label and n.w > 0})
    except Exception as e:  # noqa: BLE001 - diagnostics must never mask the real failure
        labels = [f"(accessibility tree unavailable: {e})"]
    (run.path / f"{action}-labels.txt").write_text("\n".join(labels) + "\n")


def _add_cgm_once(drv: Driver, run: Run | None, via_settings: bool) -> None:
    settle(drv, run)
    if via_settings or not _open_picker_from_home(drv, run, "Add CGM", 0.5):
        open_settings(drv)
        step(run, drv, "settings", "Settings root")
        # Trio 1.0's Devices screen calls the row "Continuous Glucose Monitor"; older ones "CGM"
        _tap_seq(drv, run, ["Devices"])
        _tap_seq(drv, run, ["Continuous Glucose Monitor" if drv.find("Continuous Glucose Monitor") else "CGM"])
        dismiss_dialogs(drv, ["Allow", "OK"])
        drv.tap_text("Add CGM"); time.sleep(2)
    _tap_picker_row(drv, "Glucose Simulator")
    time.sleep(2.5)
    dismiss_dialogs(drv, ["Continue", "Done", "OK", "Save"])
    step(run, drv, "cgm-simulator-added", "After adding Glucose Simulator")
    _close_device_sheet(drv)


def _add_pump_once(drv: Driver, run: Run | None, via_settings: bool) -> None:
    settle(drv, run)
    if via_settings or not _open_picker_from_home(drv, run, "Add pump", 0.125):
        open_settings(drv)
        _tap_seq(drv, run, ["Devices", "Insulin Pump", "Add Pump"])
    _tap_picker_row(drv, "Pump Simulator")
    time.sleep(3)
    # MockPumpManager setup: keep defaults and continue until it is dismissed
    for _ in range(6):
        lab = _visible(drv, ["Continue", "Done", "Save", "OK"])
        if not lab:
            break
        drv.tap_text(lab, exact=True)
        time.sleep(1.0)
    step(run, drv, "pump-simulator-added", "Insulin Pump screen after adding Pump Simulator")
    _close_device_sheet(drv)


def _add_device(drv: Driver, run: Run | None, kind: str, shortcut: str, once, attempts: int) -> None:
    """Add a simulated device, then prove it: back on home, its 'Add …' shortcut must be gone.

    The first attempt uses the home shortcut; a failed or unverified attempt is recorded, the app is
    reset to home, and the next attempt goes through Settings ▸ Devices instead.
    """
    for attempt in range(1, attempts + 1):
        failed = False
        try:
            once(drv, run, via_settings=attempt > 1)
        except (LookupError, HarnessError) as e:
            failed = True
            _diagnose(drv, run, f"{kind}-attempt-{attempt}-failed", f"{type(e).__name__}: {e}")
        if not reset_to_home(drv, run):
            _diagnose(drv, run, f"{kind}-attempt-{attempt}-not-home", "could not get back to the home screen")
            continue
        if not drv.find(shortcut, exact=True):
            if attempt > 1:
                run and run.log_step(f"{kind}-recovered", f"{kind} simulator added on attempt {attempt}")
            return
        if not failed:
            _diagnose(drv, run, f"{kind}-attempt-{attempt}-unverified", f"home still offers '{shortcut}'")
    raise HarnessError(f"could not add the {kind} simulator after {attempts} attempts; "
                       f"see the *-attempt-* screenshots and labels in the run")


def add_cgm_simulator(drv: Driver, run: Run | None = None, attempts: int = 2) -> None:
    _add_device(drv, run, "cgm", "Add CGM", _add_cgm_once, attempts)


def add_pump_simulator(drv: Driver, run: Run | None = None, attempts: int = 2) -> None:
    _add_device(drv, run, "pump", "Add pump", _add_pump_once, attempts)


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
