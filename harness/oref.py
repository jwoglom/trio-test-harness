"""`th oref`: run one oref cycle in Trio by hand (glucose, then oref's determination, then the enactment) and check,
from Trio's own log, that it went all the way through. Named for the algorithm, not "loop", so it is not taken for the
Loop app (`--app loop`), whose cycles this does not check.

    th --target sim oref                 # force a cycle (closed loop) and check it
    th --target sim oref --no-trigger    # check the latest cycle in the log, forcing nothing
    th --target sim oref --wait          # Open Loop: wait for Trio's next cycle and check it

Where it reads (harness/applogs.py): Trio's lines from its log, logs/AppLog.sqlite in current Trio (log.txt in older
Trio); TandemKit's, shown alongside, from tandem/tandem_log.txt, else from TandemKit's entries in the device log
(logs/DeviceLog.sqlite). Both stores are copied from the phone over SSH.

Facts (Trio feat/tandem-closed-beta and dev: Trio/Sources/APS/APSManager.swift, DeviceDataManager.swift,
Modules/Home/View/HomeRootView+Refresh.swift, Config/Config.swift) this relies on:
  * A pull-down on the home screen past 70 pt forces a cycle ("Pull down to force loop"): `heartbeatNow` refreshes
    the pump's data, then runs `loop()`. Trio offers it only where its dosing mode enacts anything, so not in Open
    Loop, where cycles still run on their own (every new glucose reading) and only recommend.
  * A cycle within 3 min of the last one's start is skipped, forced or not (`Config.loopInterval`; "Loop skipped:
    already running or too soon", a debug line), so the pull waits that out.
  * Each cycle logs, in order: "Loop started, mode <mode>" (", requested by user" when forced); "Glucose ready: BG …"
    or "Glucose check failed: …"; "Determination: eventual BG …, temp basal <rate U/h for N min | none>, SMB <u> U";
    where the mode enacts, "Temp basal enacted: …", "SMB enacted: …" or "Determination not enacted: pump suspended";
    and last "Loop finished in N s" or "Loop failed" with the error.
  * The dosing mode is `dosingMode` in Documents/freeaps/freeaps_settings.json (closed | open | lowGlucoseSuspend |
    basalTesting; only open enacts nothing), or the older `closedLoop` boolean.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from dataclasses import dataclass, field

from . import applogs as A
from .util import HarnessError, log

SETTINGS = "freeaps/freeaps_settings.json"
LOOP_INTERVAL = 180            # s: Config.loopInterval
STARTED = re.compile(r"Loop started, mode (\w+)(, requested by user)?")
GLUCOSE_OK = re.compile(r"Glucose ready: (.*)")
GLUCOSE_BAD = re.compile(r"Glucose check failed: (.*)")
DETERMINATION = re.compile(r"Determination: eventual BG (.*?), temp basal (.*?), SMB ([\d.]+) U")
TEMP_ENACTED = re.compile(r"Temp basal enacted: (.*)")
SMB_ENACTED = re.compile(r"SMB enacted: (.*)")
NOT_ENACTED = re.compile(r"Determination not enacted: (.*)")
FINISHED = re.compile(r"Loop finished in ([\d.]+) s")
FAILED = re.compile(r"Loop failed")
SKIPPED = re.compile(r"Loop skipped: (.*)")
# TandemKit's side of an enactment, shown alongside
PUMP_LINES = re.compile(r"enactTempBasal|enactBolus|[Tt]emp basal (enacted|cancelled)|SetTempRate|StopTempRate|"
                        r"InitiateBolus|refus")

_now = lambda: dt.datetime.now().astimezone()   # noqa: E731 - replaced in tests
_sleep = time.sleep


def dosing_mode(target: str, bundle: str) -> str | None:
    """Trio's dosing mode from its settings file; None when it cannot be read."""
    from . import appdebug
    try:
        data = json.loads(appdebug.decode_file(SETTINGS, appdebug.call(target, "read_file", bundle, SETTINGS)))
    except (HarnessError, ValueError) as e:
        log(f"dosing mode unknown: {e}")
        return None
    if isinstance(data.get("dosingMode"), str):
        return data["dosingMode"]
    if isinstance(data.get("closedLoop"), bool):
        return "closed" if data["closedLoop"] else "open"
    return None


def _from(rx: re.Pattern, text: str) -> str:
    """The entry from where `rx` matches, on one line: the message without the log's own prefix."""
    m = rx.search(text)
    return " ".join((text[m.start():] if m else text).split())[:400]


def _entries(target: str, bundle: str, name: str = "trio") -> list[tuple[dt.datetime, str]]:
    got = A.fetch(target, bundle, A.FILES[name])
    texts = [got.get(rel) for rel in A.FILES[name]]
    if name == "tandem" and not any(texts):
        texts = [A.tandem_from_device(A.fetch(target, bundle, A.FILES["device"]))]
    out = [(when, text) for t in texts for when, _, text in A._entries(t or "") if when]
    return sorted(out, key=lambda e: e[0])


@dataclass
class Cycle:
    start: tuple[dt.datetime, str] | None = None
    mode: str | None = None
    forced: bool = False
    glucose: str | None = None
    glucose_ok: bool | None = None
    determination: str | None = None
    temp: str | None = None          # the determination's temp basal: "none" or "<rate> U/h for <n> min"
    smb: float = 0.0
    enacted: list[str] = field(default_factory=list)
    not_enacted: str | None = None
    end: tuple[dt.datetime, str] | None = None
    failed: bool = False
    skipped: list[str] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return self.end is not None


def find_cycle(entries: list[tuple[dt.datetime, str]], since: dt.datetime, *, forced: bool | None = None,
               latest: bool = False) -> Cycle:
    """The first cycle that starts at or after `since` (the latest with `latest`), `forced` (requested by user) when
    asked, read up to its "Loop finished" or "Loop failed"."""
    c = Cycle()
    starts = [i for i, (w, t) in enumerate(entries) if w >= since and (m := STARTED.search(t))
              and (forced is None or bool(m.group(2)) == forced)]
    c.skipped = [SKIPPED.search(t).group(1) for w, t in entries if w >= since and SKIPPED.search(t)]
    if not starts:
        return c
    i = starts[-1] if latest else starts[0]
    m = STARTED.search(entries[i][1])
    c.start, c.mode, c.forced = entries[i], m.group(1), bool(m.group(2))
    for w, t in entries[i + 1:]:
        if STARTED.search(t):
            break                      # the next cycle: this one never logged its end
        if m := GLUCOSE_OK.search(t):
            c.glucose, c.glucose_ok = m.group(1), True
        elif m := GLUCOSE_BAD.search(t):
            c.glucose, c.glucose_ok = m.group(1), False
        elif m := DETERMINATION.search(t):
            c.determination, c.temp, c.smb = m.group(0), m.group(2), float(m.group(3))
        elif TEMP_ENACTED.search(t) or SMB_ENACTED.search(t):
            c.enacted.append(_from(TEMP_ENACTED if TEMP_ENACTED.search(t) else SMB_ENACTED, t))
        elif m := NOT_ENACTED.search(t):
            c.not_enacted = m.group(1)
        elif FINISHED.search(t) or FAILED.search(t):
            c.end, c.failed = (w, t), bool(FAILED.search(t))
            break
    return c


def verdict(c: Cycle, mode: str | None) -> tuple[bool, list[str]]:
    """Whether the cycle went all the way through, and a line per stage (✓ / ✗ / ·)."""
    lines: list[str] = []
    ok = True

    def stage(good: bool | None, text: str) -> None:
        nonlocal ok
        lines.append(f"{'✓' if good else '·' if good is None else '✗'} {text}")
        ok = ok and good is not False

    enacts = (mode or c.mode) not in ("open", None)
    stage(c.start is not None, f"started: {_from(STARTED, c.start[1]) if c.start else 'no cycle started'}")
    if not c.start:
        return False, lines
    stage(c.glucose_ok, f"glucose: {c.glucose or 'not logged'}")
    stage(c.determination is not None, f"determination: {c.determination or 'not logged'}")
    if enacts and not c.failed:
        wants_temp = bool(c.temp) and c.temp != "none"
        wants_smb = c.smb > 0
        temp_done = any(TEMP_ENACTED.search(e) for e in c.enacted)
        smb_done = any(SMB_ENACTED.search(e) for e in c.enacted)
        if c.not_enacted:
            stage(None, f"enacted: nothing ({c.not_enacted})")
        elif not (wants_temp or wants_smb):
            stage(True, "enacted: nothing to change (the determination kept the current basal)")
        else:
            missing = [w for w, want, done in (("the temp basal", wants_temp, temp_done), ("the SMB", wants_smb, smb_done))
                       if want and not done]
            stage(not missing, "enacted: " + ("; ".join(c.enacted) if not missing
                                              else f"{' and '.join(missing)} the determination asked for was not logged as enacted"))
    elif not enacts:
        stage(None, f"enacted: not expected ({mode or c.mode or 'unknown'} mode: Trio only recommends)")
    if c.end:
        stage(not c.failed, ("failed: " if c.failed else "finished: ") + _from(FAILED if c.failed else FINISHED, c.end[1]))
    else:
        stage(False, "finished: no \"Loop finished\" or \"Loop failed\" logged yet")
    return ok, lines


def run_cycle(drv, target: str, bundle: str, flows, run=None, *, trigger: bool = True, wait_next: bool = False,
              timeout: float = 60) -> tuple[bool, str]:
    """Forces one cycle (or takes the latest / the next) and checks it; returns (ok, report)."""
    mode = dosing_mode(target, bundle)
    enacts = mode not in ("open",)          # unknown: try the pull anyway
    since = _now() - dt.timedelta(seconds=2)
    notes = [f"dosing mode: {mode or 'unknown'}"]
    forced = None
    if trigger and enacts:
        last = find_cycle(_entries(target, bundle), since - dt.timedelta(minutes=10), latest=True)
        if last.start and (wait := LOOP_INTERVAL + 3 - (_now() - last.start[0]).total_seconds()) > 0:
            log(f"the last cycle started {LOOP_INTERVAL + 3 - wait:.0f} s ago; waiting {wait:.0f} s for Trio's "
                f"{LOOP_INTERVAL // 60}-minute loop interval")
            _sleep(wait)
        if not flows.at_home(drv):
            flows.back_to_home(drv)
        since, forced = _now() - dt.timedelta(seconds=2), True
        for attempt in (1, 2):
            pull_down(drv)
            c = _wait(target, bundle, since, forced=True, until_start=True, timeout=20)
            if c.start:
                break
            log(f"the pull-down did not start a cycle (attempt {attempt} of 2)")
        else:
            c = Cycle()
            notes.append("the pull-down on the home screen did not start a cycle")
    elif trigger and wait_next:
        notes.append("Open Loop: Trio offers no forced cycle; waiting for its next one")
        c = _wait(target, bundle, since, until_start=True, timeout=max(timeout, 6 * 60))
    else:
        if trigger:
            notes.append("Open Loop: Trio offers no forced cycle; checking its latest one (--wait for the next)")
        c = find_cycle(_entries(target, bundle), since - dt.timedelta(minutes=10), latest=True)
    if c.start and not c.done:
        c = _wait(target, bundle, c.start[0], forced=forced, timeout=timeout)
    ok, lines = verdict(c, mode)
    if c.skipped and not c.start:
        lines.append(f"· Trio skipped: {'; '.join(c.skipped)}")
    if c.start:
        pump = [t.strip().splitlines()[0] for w, t in _entries(target, bundle, "tandem")
                if w >= c.start[0] and (not c.end or w <= c.end[0] + dt.timedelta(seconds=5)) and PUMP_LINES.search(t)]
        if pump:
            lines.append("TandemKit during the cycle:")
            lines += [f"    {p[:300]}" for p in pump[-12:]]
    report = "\n".join([f"Trio oref cycle: {'PASSED' if ok else 'FAILED'} ({'; '.join(notes)})", *lines])
    if run is not None:
        try:
            flows.step(run, drv, "oref-cycle", f"Trio oref cycle {'passed' if ok else 'failed'}: "
                       + "; ".join(l[2:] for l in lines if l[:1] in "✓✗")[:900])
        except Exception as e:  # noqa: BLE001 - the check stands without its screenshot
            log(f"no screenshot of the oref cycle: {e}")
    return ok, report


def _wait(target: str, bundle: str, since: dt.datetime, *, forced: bool | None = None, until_start: bool = False,
          timeout: float = 120) -> Cycle:
    deadline = time.monotonic() + timeout
    while True:
        c = find_cycle(_entries(target, bundle), since, forced=forced)
        if (c.start if until_start else c.done) or time.monotonic() > deadline:
            return c
        _sleep(3)


def pull_down(drv) -> None:
    """The home screen's pull-down: from near its top, 35% of the screen down (~300 pt on an iPhone), well past the
    70 pt that arms a forced cycle."""
    w, h = drv.screen_size()
    drv.swipe(w // 2, int(h * 0.22), w // 2, int(h * 0.57), duration=0.3)
