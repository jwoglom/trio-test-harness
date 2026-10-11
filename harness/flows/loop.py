"""Loop-specific UI flows: the functions of flows/trio.py, for Loop built from LoopWorkspace. They reuse Trio's
screen-reading helpers where the screens agree. Heuristics, as Trio's are: prefer `th shot` + `th tap-text` when a
flow does not fit the screen it sees.

Facts (LoopWorkspace dev and feat/tandem-closed-beta) these flows rely on:
  * Onboarding state is in the app group's UserDefaults (the suite Info.plist's AppGroupIdentifier names):
    com.loopkit.Loop.OnboardingManager.IsComplete, which OnboardingManager honours only with complete therapy settings.
    On the simulator `preseed_simulator` writes both, the settings as the legacy keys SettingsManager migrates at
    launch, then restarts cfprefsd. A phone walks: holding the welcome screen's logo for 2 s skips onboarding with
    LoopOnboarding's mock therapy settings, the values preseeding writes too.
  * Then, until each is answered: notifications (iOS alert, "Allow"), Apple Health (a sheet: "Turn On All", then
    "Allow" at its top right) and Bluetooth (iOS alert), before the home screen.
  * A dev-branch build warns at launch ("Warning", "I'm a tester"); the harness builds with
    DEV_BRANCH_WARNING_DISABLED, which Loop versions with that warning honour.
  * Home is the status screen: a HUD (glucose, loop, pump; "Add CGM" / "Add Pump" while missing), charts titled
    Glucose, Active Insulin, Insulin Delivery and Active Carbohydrates, and a toolbar of icons whose labels (Add Meal,
    Pre-Meal Targets, Bolus, Workout Targets, Settings) only the accessibility tree has.
  * The HUD's "Add CGM" / "Add Pump" opens an action sheet of that title listing the plugins; "CGM Simulator" and
    "Pump Simulator" are set up at once, with no screens. Settings (toolbar) offers the same choosers; "Done" closes it.
  * A new CGM Simulator has no glucose model, so no readings: on the simulator `give_cgm_glucose` writes a sine curve
    into Documents/CGMManagerState.plist and relaunches Loop; on a phone, tap its HUD ▸ Simulator Settings ▸ Model.
"""
from __future__ import annotations

import plistlib
import time
from pathlib import Path

from ..driver import Driver
from ..report import Run
from ..util import tmp_path, HarnessError, log
from . import trio as T
from .trio import step, _visible, _tap_label, _diagnose, _tap_picker_row

NAME = "Loop"
CGM_SIMULATOR = "CGM Simulator"
PUMP_SIMULATOR = "Pump Simulator"
ADD_CGM = "Add CGM"
ADD_PUMP = "Add Pump"
PROMPTS_AFTER_PRESEED = True       # Loop asks for its permissions after onboarding, which walk_onboarding answers
WELCOME = "Welcome to Loop"
TESTER = "I'm a tester"
HOME_MARKERS = ["Add Meal", "Bolus", "Settings", "Active Insulin", "Insulin Delivery", "Active Carbohydrates"]
# Over the home screen, whose labels stay in the accessibility tree behind them.
BLOCKERS = ["Turn On All", "Don't Allow", TESTER]
INTERSTITIAL_BUTTONS = [*T.INTERSTITIAL_BUTTONS, TESTER]
PERMISSION_LABELS = ["Allow", "Allow While Using App", "OK", TESTER]

ONBOARDING_COMPLETE = "com.loopkit.Loop.OnboardingManager.IsComplete"


def therapy_settings(*, dosing_enabled: bool = False, dosing_strategy: int = 0, basal_rate: float = 1.2,
                     max_basal: float = 6.0, max_bolus: float = 8.0) -> dict:
    """Therapy settings as the legacy group-suite keys Loop's SettingsManager migrates at launch (LoopCore
    NSUserDefaults.swift). The defaults are LoopOnboarding's mock settings
    (OnboardingUICoordinator.mockTherapySettingsAndSkipOnboarding), open loop; `dosing_strategy` is
    AutomaticDosingStrategy: 0 temp basal only, 1 automatic bolus."""
    import datetime as dt
    offset = int(dt.datetime.now().astimezone().utcoffset().total_seconds())

    def schedule(value, unit: str | None = None) -> dict:
        raw = {"timeZone": offset, "items": [{"startTime": 0.0, "value": value}]}
        if unit:
            raw["unit"] = unit
        return raw
    return {
        "com.loopkit.Loop.loopSettings": {
            "version": 1,
            "dosingEnabled": dosing_enabled,
            "overridePresets": [],
            "glucoseTargetRangeSchedule": schedule([105.0, 110.0], "mg/dL"),
            "maximumBasalRatePerHour": float(max_basal),
            "maximumBolus": float(max_bolus),
            "minimumBGGuard": {"units": "mg/dL", "value": 75.0},
            "dosingStrategy": dosing_strategy,
        },
        "com.loudnate.Naterade.BasalRateSchedule": schedule(float(basal_rate)),
        "com.loudnate.Naterade.InsulinSensitivitySchedule": schedule(50.0, "mg/dL"),
        "com.loudnate.Naterade.CarbRatioSchedule": schedule(15.0, "g"),
    }


# ---------------- simulator pre-seeding -----------------

def _app_group(drv, bundle_id: str) -> tuple[str, Path]:
    groups = drv.app_group_containers(bundle_id)
    if not groups:
        raise HarnessError(f"{bundle_id} has no app group container on the simulator (built without its app group?)")
    group = next((g for g in groups if g.endswith("Group")), next(iter(groups)))
    return group, Path(groups[group])


def reset_after_install(drv, bundle_id: str) -> None:
    """The simulator keeps an app group container, with Loop's store and settings in it, across reinstalls: empty it,
    keeping the metadata that keeps it registered, so a new install starts afresh as it does on a phone."""
    import shutil
    drv.terminate(bundle_id)
    _, root = _app_group(drv, bundle_id)
    for child in root.iterdir():
        if child.name == ".com.apple.mobile_container_manager.metadata.plist":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    drv.drop_preferences_cache()


def preseed_simulator(drv, bundle_id: str, *, settings: dict | None = None) -> list[Path]:
    """Mark onboarding complete with `settings` (therapy_settings(); default LoopOnboarding's mock ones) on the
    simulator. On an install Loop has run in, its therapy settings are replaced at its next launch."""
    drv.terminate(bundle_id)
    group, root = _app_group(drv, bundle_id)
    path = root / "Library" / "Preferences" / f"{group}.plist"
    values = plistlib.loads(path.read_bytes()) if path.exists() else {}
    values.update(settings or therapy_settings())
    values[ONBOARDING_COMPLETE] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(values, fmt=plistlib.FMT_BINARY))
    drv.drop_preferences_cache()
    return [path]


def preseed_phone(bundle_id: str, *, onboarding: bool = True) -> bool:
    """Loop's onboarding state is in its app group's preferences, which cfprefsd holds: a phone walks instead."""
    return False


CGM_STATE_FILE = "Documents/CGMManagerState.plist"


def sine_curve_source(*, base: float = 110.0, amplitude: float = 20.0, period_hours: float = 6.0,
                      frequency: int = 0) -> dict:
    """A CGM Simulator data source (MockCGMDataSource raw values): glucose in mg/dL, `frequency` MeasurementFrequency
    (0 a reading every 5 minutes, 1 every minute)."""
    return {"model": {"kind": "sineCurve", "baseGlucose": float(base), "amplitude": float(amplitude),
                      "period": period_hours * 3600.0, "referenceDate": time.time()},
            "effects": {}, "dataPointFrequency": frequency}


def write_cgm_simulator(container: Path, data_source: dict) -> Path:
    """A CGM Simulator Loop restores at launch (its PersistedProperty CGMManagerState), before Loop has run."""
    path = container / CGM_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({"managerIdentifier": "MockCGMManager", "state": {"dataSource": data_source}},
                                    fmt=plistlib.FMT_BINARY))
    return path


def give_cgm_glucose(drv, bundle_id: str, *, base: float = 110.0, amplitude: float = 20.0) -> bool:
    """Give an added CGM Simulator a sine-curve glucose model and relaunch Loop to read it (simulator only). False
    when there is no CGM Simulator to change."""
    path = drv.app_data_container(bundle_id) / CGM_STATE_FILE
    drv.terminate(bundle_id)
    raw = plistlib.loads(path.read_bytes()) if path.exists() else None
    if not raw or raw.get("managerIdentifier") != "MockCGMManager":
        drv.launch(bundle_id)
        return False
    raw.setdefault("state", {})["dataSource"] = sine_curve_source(base=base, amplitude=amplitude)
    path.write_bytes(plistlib.dumps(raw, fmt=plistlib.FMT_BINARY))
    drv.launch(bundle_id)
    return True


# ---------------- screens -----------------

def offers(drv: Driver, label: str) -> bool:
    """`label` on screen: in the accessibility tree, or read off a screenshot where there is no tree (a phone)."""
    if drv.find(label, exact=True):
        return True
    return not drv.ui_tree() and bool(T._ocr_hits(drv, drv.screenshot(tmp_path("offers.png")), label))


def at_home(drv: Driver, shot: Path | None = None) -> bool:
    nodes = drv.ui_tree()
    if nodes:
        home = sum(1 for m in HOME_MARKERS if any(n.matches(m, exact=True) for n in nodes)) >= 2
    else:
        shot = shot or drv.screenshot(tmp_path("home.png"))
        home = sum(1 for m in HOME_MARKERS if T._ocr_hits(drv, shot, m)) >= 2
    return home and not _visible(drv, BLOCKERS, shot)


def _logo_point(drv: Driver, shot: Path) -> tuple[int, int]:
    """The welcome screen's logo: between the title and the text under it."""
    w, h = drv.screen_size()
    nodes = drv.ui_tree()
    if nodes:
        title = next((n for n in nodes if n.matches(WELCOME, exact=True) and n.w > 0), None)
        body = next((n for n in nodes if n.label.startswith("Before using Loop") and n.w > 0), None)
        top, bottom = (title.y + title.h if title else None), (body.y if body else None)
    else:
        from ..ocr import find_text
        title = next(iter(find_text(shot, WELCOME)), None)
        body = next(iter(find_text(shot, "Before using Loop")), None)
        top, bottom = (title.y + title.h if title else None), (body.y if body else None)
    if top is None and bottom is None:
        raise HarnessError("welcome screen: neither its title nor its text found; hold the logo by hand (th tap --duration 3)")
    if top is None:
        top = bottom - int(h * 0.2)
    if bottom is None or bottom <= top:
        bottom = top + int(h * 0.2)
    return w // 2, (top + bottom) // 2


def _health_sheet(drv: Driver, run: Run | None, shot: Path | None = None) -> bool:
    """Apple Health's access sheet: turn every category on, then Allow (top right; disabled until one is on)."""
    if not _visible(drv, ["Turn On All"], shot):
        return False
    step(run, drv, "health-access", "Apple Health access sheet") if run else None
    drv.tap_text("Turn On All", exact=True)
    time.sleep(1.0)
    w, h = drv.screen_size()
    tops = [n for n in drv.find("Allow", exact=True) if n.w > 0 and n.center[1] < h * 0.2]
    if tops:
        drv.tap(*tops[0].center)
    else:
        from ..ocr import find_text
        s = drv.screenshot(tmp_path("health.png"))
        hits = [x for x in find_text(s, "Allow", isolated=True) if x.center[1] < h * 0.2]
        if not hits:
            raise HarnessError("Apple Health sheet: 'Allow' not found at its top")
        drv.tap(*hits[0].center)
    log("step: Apple Health access allowed")
    time.sleep(1.5)
    return True


def settle(drv: Driver, run: Run | None = None, *, rounds: int = 6, labels: list[str] | None = None) -> int:
    """Clear interstitials (Apple Health's sheet, permission alerts, the dev-build warning) until none shows."""
    n = 0
    for _ in range(rounds):
        if _health_sheet(drv, run):
            n += 1
            continue
        done = T.settle(drv, run, rounds=1, labels=labels or INTERSTITIAL_BUTTONS)
        if not done:
            return n
        n += done
    return n


def dismiss_dialogs(drv: Driver, labels: list[str] | None = None, rounds: int = 3) -> int:
    return settle(drv, rounds=rounds, labels=labels)


def walk_onboarding(drv: Driver, run: Run | None = None, *, max_steps: int = 20, settle_s: float = 1.0) -> bool:
    """Skip Loop's onboarding from its welcome screen (hold the logo), answer the permission prompts that follow, and
    stop at the home screen. Loop's full onboarding (therapy settings editors) is not walked. True when the welcome
    screen showed: Loop then runs on LoopOnboarding's mock settings, not pre-seeded ones."""
    held = 0
    for i in range(max_steps):
        time.sleep(settle_s)
        shot = run.shot_path(f"onboarding-{i:02d}") if run else tmp_path("flow.png")
        drv.screenshot(shot)
        if at_home(drv, shot):
            if run:
                run.log_step("home", "Reached the home screen", shot)
            log("step: home")
            return held > 0
        if _visible(drv, [WELCOME], shot):
            if held == 3:
                raise HarnessError("onboarding: holding the welcome screen's logo did not skip onboarding; see the "
                                   "run screenshots, and hold it by hand with `th tap X Y --duration 3`")
            x, y = _logo_point(drv, shot)
            if run:
                run.log_step(f"onboarding-{i:02d}", f"hold the logo at {x},{y} (skips onboarding with mock settings)", shot)
            log(f"step: onboarding-{i:02d} hold the logo at {x},{y}")
            drv.tap(x, y, duration=3.0)
            held += 1
            time.sleep(1.5)
            continue
        if _health_sheet(drv, run, shot):
            continue
        label = _visible(drv, PERMISSION_LABELS, shot)
        if label is None:
            if run:
                run.log_step(f"onboarding-{i:02d}-stuck", "No welcome screen, permission prompt or home screen", shot)
            raise HarnessError("onboarding: not on Loop's welcome screen, a permission prompt or home; drive manually "
                               "with th shot / th tap-text")
        if run:
            run.log_step(f"onboarding-{i:02d}", f"tap '{label}'", shot)
        log(f"step: onboarding-{i:02d} tap '{label}'")
        _tap_label(drv, label, shot)
    raise HarnessError("onboarding: exceeded max steps")


def open_settings(drv: Driver) -> None:
    """Loop's Settings sheet, from the toolbar's last item."""
    settle(drv)
    nodes = [n for n in drv.find("Settings", exact=True) if n.w > 0]
    if nodes:
        w, h = drv.screen_size()
        bar = [n for n in nodes if n.y > h * 0.8] or nodes
        drv.tap(*bar[-1].center)
    else:
        w, h = drv.screen_size()
        drv.tap(int(w * 0.9), int(h * 0.95))     # icon-only toolbar: Settings is the rightmost item
    time.sleep(1.5)


def devices_configured(drv: Driver) -> bool:
    return at_home(drv) and not offers(drv, ADD_CGM) and not offers(drv, ADD_PUMP)


def _sheet_open(drv: Driver) -> str | None:
    return _visible(drv, ["Done", "Cancel", "Close"]) if drv.ui_tree() else None


def reset_to_home(drv: Driver, run: Run | None = None, attempts: int = 6) -> bool:
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


def back_to_home(drv: Driver, hops: int = 4) -> None:
    """Close sheets and pop navigation until the status screen shows."""
    for _ in range(hops):
        if at_home(drv) and not _sheet_open(drv):
            return
        for lab in ("Done", "Cancel", "Close", "Back"):
            try:
                drv.tap_text(lab, exact=True, ocr_fallback=False)
                break
            except LookupError:
                continue
        else:
            w, h = drv.screen_size()
            drv.tap(int(w * 0.06), int(h * 0.075))
        time.sleep(0.8)


def _add_once(drv: Driver, run: Run | None, via_settings: bool, add: str, row: str, kind: str) -> None:
    settle(drv, run)
    if via_settings or not offers(drv, add):
        open_settings(drv)
        step(run, drv, "settings", "Settings")
        # the HUD's label stays in the tree behind the sheet; Settings' button is the lower one
        nodes = sorted((n for n in drv.find(add) if n.w > 0), key=lambda n: n.y)
        drv.tap(*nodes[-1].center) if nodes else drv.tap_text(add)
    else:
        drv.tap_text(add)
    time.sleep(1.5)
    step(run, drv, f"{kind}-chooser", f"'{add}' chooser")
    _tap_picker_row(drv, row)
    time.sleep(3)
    settle(drv, run)
    step(run, drv, f"{kind}-simulator-added", f"after choosing {row}")


def _add_device(drv: Driver, run: Run | None, kind: str, add: str, row: str, attempts: int) -> None:
    """Add a simulated device, then prove it: back on home, its 'Add …' highlight must be gone. The first attempt goes
    through the HUD, a later one through Settings."""
    for attempt in range(1, attempts + 1):
        failed = False
        try:
            _add_once(drv, run, attempt > 1, add, row, kind)
        except (LookupError, HarnessError) as e:
            failed = True
            _diagnose(drv, run, f"{kind}-attempt-{attempt}-failed", f"{type(e).__name__}: {e}")
        if not reset_to_home(drv, run):
            _diagnose(drv, run, f"{kind}-attempt-{attempt}-not-home", "could not get back to the home screen")
            continue
        if not offers(drv, add):
            if attempt > 1 and run:
                run.log_step(f"{kind}-recovered", f"{row} added on attempt {attempt}")
            return
        if not failed:
            _diagnose(drv, run, f"{kind}-attempt-{attempt}-unverified", f"home still offers '{add}'")
    raise HarnessError(f"could not add the {row} after {attempts} attempts; see the *-attempt-* screenshots and labels "
                       "in the run")


def add_cgm_simulator(drv: Driver, run: Run | None = None, attempts: int = 2) -> None:
    _add_device(drv, run, "cgm", ADD_CGM, CGM_SIMULATOR, attempts)


def add_pump_simulator(drv: Driver, run: Run | None = None, attempts: int = 2) -> None:
    _add_device(drv, run, "pump", ADD_PUMP, PUMP_SIMULATOR, attempts)
