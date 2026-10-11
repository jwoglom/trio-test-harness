"""`th logs`: read Trio's and TandemKit's log files from the installed app's data container. Loop writes no text log
of its own (its device log is the SQLite store Documents/DeviceLog/Storage.sqlite), so with --app loop only
TandemKit's is there.

    th --target rooted-vnc logs tandem --grep bolus           # today's TandemKit log, matching entries
    th logs trio --since 10m                                    # Trio's log, the last 10 minutes
    th logs tandem --level warning -n 0                         # every warning and error (today and yesterday)
    th logs all --save                                          # copy every log file into the current run's logs/

Files, under the container's Documents/ (see Trio's SimpleLogReporter and TandemKit's TandemLogger):
  trio    logs/log.txt          (+ logs/log_prev.txt, the previous day)
  watch   logs/watch_log.txt    (+ logs/watch_log_prev.txt)
  tandem  tandem/tandem_log.txt (+ tandem/tandem_log_prev.txt); only info/warning/error reach the file, debug
          messages go to OSLog (subsystem com.jwoglom.TandemKit) only.
Each file starts at local midnight and is rotated to *_prev once a day; both are read, so the logs reach back to the
start of the previous day at most. Output is the last -n entries (default 200) after the filters.

Simulator: read straight from the simctl data container. Phone (rooted-vnc): over SSH. When the harness daemon for the
target is running, the daemon reads the files, so the command also works where SSH is not reachable (the web UI/CI
agent's sandbox allows only the daemon's socket).
"""
from __future__ import annotations

import datetime as dt
import re
import shlex
from pathlib import Path

from .util import HarnessError

FILES = {
    "trio": ["logs/log_prev.txt", "logs/log.txt"],
    "tandem": ["tandem/tandem_log_prev.txt", "tandem/tandem_log.txt"],
    "watch": ["logs/watch_log_prev.txt", "logs/watch_log.txt"],
}
APP_FILES = {"trio": ("trio", "tandem", "watch"), "loop": ("tandem",)}


def files_for(app: str) -> dict[str, list[str]]:
    """The log files the app (harness/apps.py key) writes, by name."""
    return {name: FILES[name] for name in APP_FILES.get(app, tuple(FILES))}


# Trio:      2026-09-25T23:09:30-0400 [deviceManager] File.swift - func - 12 - message
# TandemKit: [2026-09-25T23:09:30-0400 WARNING] File.swift - func#12: message
ENTRY = re.compile(r"^\[?(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4})(?: (INFO|WARNING|ERROR|DEFAULT|DEBUG)\])?")
LEVELS = {"info": 0, "warning": 1, "error": 2}
MARK = "@@th-logs@@"


def read_files(target: str, bundle_id: str, relpaths: list[str]) -> dict[str, str | None]:
    """relpath (under Documents/) -> contents, or None if the file does not exist. Runs where the device is reachable
    (this process, or the daemon)."""
    out: dict[str, str | None] = {}
    if target == "sim":
        from . import xcode
        cp = xcode.xcrun(["simctl", "get_app_container", xcode.ensure_simulator()["udid"], bundle_id, "data"],
                         quiet=True, check=False)
        if cp.returncode != 0:
            raise HarnessError(f"{bundle_id} is not installed on the harness simulator")
        docs = Path(cp.stdout.strip()) / "Documents"
        for rel in relpaths:
            f = docs / rel
            out[rel] = f.read_text(errors="replace") if f.is_file() else None
        return out
    if target == "rooted-vnc":
        from . import devices as D
        container = D.ssh_app_container(bundle_id)
        script = "; ".join(
            f"echo {MARK}{shlex.quote(rel)}; p={shlex.quote(f'{container}/Documents/{rel}')}; "
            f"if [ -f \"$p\" ]; then cat \"$p\"; echo; else echo {MARK}-missing; fi" for rel in relpaths)
        cp = D.ssh_run(script, check=False, timeout=120)
        if MARK not in cp.stdout:
            raise HarnessError(f"reading the logs over SSH failed: {(cp.stderr or cp.stdout)[-500:] or 'no output'}")
        for chunk in re.split(rf"^{re.escape(MARK)}", cp.stdout, flags=re.M)[1:]:
            name, _, body = chunk.partition("\n")
            if name == "-missing":
                out[rel] = None
            else:
                rel = name
                out[rel] = body[:-1] if body.endswith("\n") else body     # the echo after cat
        return out
    raise HarnessError(f"th logs: target {target} is not supported (sim or rooted-vnc)")


def default_bundle(target: str, app: str | None = None) -> str:
    """The app's last build's bundle id when it was built for this target, else the target's configured one."""
    from . import apps
    from .build import BuildInfo
    from .config import CONFIG
    a = apps.get(app) if app else apps.current()
    bi = BuildInfo.load(a)
    built_for = "sim" if bi and bi.target == "sim" else "rooted-vnc" if bi and str(bi.target).startswith("device") else None
    return bi.bundle_id if bi and built_for == target else a.bundle_id(CONFIG.team_for(target))


_CONTAINERS: dict[tuple[str, str], str] = {}


def read_tail(target: str, bundle_id: str, rel: str, offset: int | None, max_bytes: int = 256 * 1024) -> dict:
    """Bytes of Documents/<rel> after `offset` (None: the last 64 KB), at most max_bytes: {"offset", "size", "text",
    "reset"}. reset=True when the file shrank (rotated at midnight) and reading restarted from the top."""
    if target == "sim":
        from . import xcode
        key = (target, bundle_id)
        if key not in _CONTAINERS:
            cp = xcode.xcrun(["simctl", "get_app_container", xcode.ensure_simulator()["udid"], bundle_id, "data"],
                             quiet=True, check=False)
            if cp.returncode != 0:
                raise HarnessError(f"{bundle_id} is not installed on the harness simulator")
            _CONTAINERS[key] = cp.stdout.strip()
        f = Path(_CONTAINERS[key]) / "Documents" / rel
        if not f.is_file():
            return {"offset": 0, "size": None, "text": "", "reset": False}
        size = f.stat().st_size
        start, reset = _start(offset, size)
        with open(f, "rb") as fh:
            fh.seek(start)
            data = fh.read(max_bytes)
        data = _whole_lines(data, len(data) >= max_bytes)
        return {"offset": start + len(data), "size": size, "text": data.decode(errors="replace"), "reset": reset}
    if target != "rooted-vnc":
        raise HarnessError(f"logs: target {target} is not supported (sim or rooted-vnc)")
    from . import devices as D
    key = (target, bundle_id)
    if key not in _CONTAINERS:
        _CONTAINERS[key] = D.ssh_app_container(bundle_id)
    p = shlex.quote(f"{_CONTAINERS[key]}/Documents/{rel}")
    # size first, then the bytes: `tail -c +N` is 1-based; BSD head/tail on the phone
    probe = D.ssh_run(f"if [ -f {p} ]; then wc -c < {p}; else echo missing; fi", check=False, timeout=30, mux=True)
    out = probe.stdout.strip()
    if out == "missing":
        return {"offset": 0, "size": None, "text": "", "reset": False}
    if probe.returncode != 0 or not out.isdigit():
        _CONTAINERS.pop(key, None)          # reinstalled apps get a new container
        raise HarnessError(f"could not read {rel} over SSH: {(probe.stderr or out)[-300:]}")
    size = int(out)
    start, reset = _start(offset, size)
    if start >= size:
        return {"offset": start, "size": size, "text": "", "reset": reset}
    cp = D.ssh_run(f"tail -c +{start + 1} {p} | head -c {max_bytes}", check=False, timeout=60, mux=True)
    text = cp.stdout or ""
    full = len(text.encode()) >= max_bytes - 4
    cut = text.rfind("\n") + 1
    if cut or not full:
        text = text[:cut]            # whole lines only: the rest (maybe a split character) is read next time
    return {"offset": start + len(text.encode()), "size": size, "text": text, "reset": reset}


def _whole_lines(data: bytes, full: bool) -> bytes:
    cut = data.rfind(b"\n") + 1
    return data[:cut] if cut or not full else data


def _start(offset: int | None, size: int) -> tuple[int, bool]:
    if offset is None:
        return max(0, size - 64 * 1024), False
    if offset > size:                        # rotated: a new day's file
        return 0, True
    return offset, False


def fetch(target: str, bundle_id: str, relpaths: list[str]) -> dict[str, str | None]:
    """read_files() through the daemon when one runs for the target (the sandboxed agent can only reach that)."""
    from . import daemon as Dm
    if Dm.is_running(target):
        try:
            return Dm.RemoteDriver(target)._call("app_files", bundle_id, relpaths)
        except HarnessError as e:
            if "app_files" not in str(e):
                raise
            try:              # a daemon started before `th logs` existed: read here if the device is reachable
                return read_files(target, bundle_id, relpaths)
            except HarnessError as e2:
                raise HarnessError(f"{e2}\n(the running {target} daemon predates `th logs`; restart it with "
                                   f"`th --target {target} daemon stop` then `daemon start`)") from None
    return read_files(target, bundle_id, relpaths)


def _entries(text: str) -> list[tuple[dt.datetime | None, str | None, str]]:
    """Split a log into entries (a line with a timestamp plus any continuation lines)."""
    entries: list[tuple[dt.datetime | None, str | None, list[str]]] = []
    for line in text.splitlines():
        m = ENTRY.match(line)
        if m or not entries:
            when = dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S%z") if m else None
            entries.append((when, (m.group(2) or "").lower() or None if m else None, [line]))
        else:
            entries[-1][2].append(line)
    return [(w, lv, "\n".join(ls)) for w, lv, ls in entries]


def parse_since(s: str) -> dt.datetime:
    m = re.match(r"^(\d+)\s*([smhd])$", s.strip())
    now = dt.datetime.now().astimezone()
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now - dt.timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[unit]: n})
    try:
        t = dt.datetime.fromisoformat(s.strip())
    except ValueError:
        raise HarnessError(f"--since {s!r}: use 30s, 10m, 2h, 1d or an ISO time (2026-09-25T14:00)") from None
    return t if t.tzinfo else t.astimezone()


def query(texts: dict[str, dict[str, str | None]], *, grep: str | None = None, ignore_case: bool = False,
          since: dt.datetime | None = None, level: str | None = None, lines: int | None = 200) -> list[str]:
    """Filtered entries of each log, oldest first; with several logs, each entry is prefixed with its log's name."""
    pat = re.compile(grep, re.I if ignore_case else 0) if grep else None
    minlvl = LEVELS[level] if level else None
    rows: list[tuple[dt.datetime | None, str, str]] = []
    for name, files in texts.items():
        for rel, text in files.items():
            if not text:
                continue
            for when, lvl, entry in _entries(text):
                if since and (when is None or when < since):
                    continue
                if minlvl is not None and LEVELS.get(lvl or "info", 0) < minlvl:
                    continue
                if pat and not pat.search(entry):
                    continue
                rows.append((when, name, entry))
    if len(texts) > 1:
        far = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        rows.sort(key=lambda r: r[0] or far)
    out = [f"[{n}] {e}" if len(texts) > 1 else e for _, n, e in rows]
    return out[-lines:] if lines else out


def save(texts: dict[str, dict[str, str | None]], dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%H%M%S")
    saved = []
    for files in texts.values():
        for rel, text in files.items():
            if text is None:
                continue
            p = dest / f"{stamp}-{rel.replace('/', '-')}"
            p.write_text(text)
            saved.append(p)
    return saved
