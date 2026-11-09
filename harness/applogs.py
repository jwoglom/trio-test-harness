"""`th logs`: the installed app's logs, as entries, whatever the app keeps them in.

    th logs trio --since 10m                          # Trio's own log, the last 10 minutes
    th --target rooted-vnc logs tandem --grep bolus   # TandemKit's entries matching "bolus"
    th logs device --grep TandemPumpManager           # the device log's entries from TandemKit
    th logs tandem --level warning -n 0               # every TandemKit warning and error
    th logs all --save                                # each log as text into the current run's logs/
    th logs --summary                                 # each log: how many entries, from when to when

The logs:
  trio    Trio's own log, debug lines included. The watch's lines are in it too, with categories "Watch/…".
  tandem  TandemKit's: info, warnings and errors (its debug lines go to the system log only: `th oslog`).
  device  the device log: every pump and CGM driver's entries, each as "[driver/type] message".
  watch   the Watch app's log, as far as the watch has sent it to the phone (every 3 min or 100 entries).
Loop has tandem and device only. Each log reaches back about two days. The output is the last -n entries (default
200) after the filters; `all` merges the logs by time and prefixes each entry with its log's name.

This works on the simulator and on the phone, and goes through the target's daemon when one runs, so the web UI/CI
agent, whose sandbox reaches only the daemon, can use it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import shlex
from pathlib import Path

from .util import HarnessError

# Where each log is kept, under the app container's Documents/. All of a log's sources are read and merged, oldest
# first, so no caller deals with them. Trio: Logger/Store/AppLogStore, DeviceLog/DeviceLogStore; TandemKit:
# TandemLogger and TandemLogFileWriter.
#   trio    current Trio: logs/AppLog.sqlite, a Core Data store. Older Trio wrote logs/log.txt (+ log_prev.txt), which
#           current Trio deletes after 7 days.
#   device  LoopKit's PersistentDeviceLog: Trio's logs/DeviceLog.sqlite, Loop's DeviceLog/Storage.sqlite. TandemKit
#           sends its info/warning/error lines there as "delegate" entries (TandemKit 56dcb3c on).
#   tandem  tandem/tandem_log.txt (+ _prev); where neither file is there, TandemKit's entries in the device log.
#   watch   logs/watch_log.txt (+ _prev).
# Text files start at local midnight and rotate to *_prev once a day; the stores are read for their last SQLITE_DAYS.
SOURCES = {
    "trio": ["logs/log_prev.txt", "logs/log.txt", "logs/AppLog.sqlite"],
    "tandem": ["tandem/tandem_log_prev.txt", "tandem/tandem_log.txt"],
    "device": ["logs/DeviceLog.sqlite", "DeviceLog/Storage.sqlite"],
    "watch": ["logs/watch_log_prev.txt", "logs/watch_log.txt"],
}
APP_LOGS = {"trio": ("trio", "tandem", "device", "watch"), "loop": ("tandem", "device")}
# the live pane follows one source at a time, the current one first; True: TandemKit's entries of the device log only
TAIL_SOURCES = {
    "trio": [("logs/AppLog.sqlite", False), ("logs/log.txt", False)],
    "tandem": [("tandem/tandem_log.txt", False), ("logs/DeviceLog.sqlite", True), ("DeviceLog/Storage.sqlite", True)],
    "device": [("logs/DeviceLog.sqlite", False), ("DeviceLog/Storage.sqlite", False)],
    "watch": [("logs/watch_log.txt", False)],
}
TANDEM_MANAGER = re.compile(r"^\[[^\]]* [A-Z]+\] \[[^/\]]*tandem[^/\]]*/", re.I)


def logs_for(app: str) -> tuple[str, ...]:
    """The logs the app (harness/apps.py key) has."""
    return APP_LOGS.get(app, tuple(SOURCES))


# Trio:      2026-09-25T23:09:30-0400 [deviceManager] File.swift - func - 12 - message
# TandemKit: [2026-09-25T23:09:30-0400 WARNING] File.swift - func#12: message
ENTRY = re.compile(r"^\[?(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4})(?: (INFO|WARNING|ERROR|DEFAULT|DEBUG)\])?")
LEVELS = {"info": 0, "warning": 1, "error": 2}
MARK = "@@th-logs@@"

SQLITE_DAYS = 2
CORE_DATA_EPOCH = 978307200          # 2001-01-01 UTC: Core Data stores dates as seconds since then
# Trio's LogLevel raw values (Logger/Contracts/LogRecord.swift) -> the level words ENTRY reads
SQLITE_LEVELS = {0: "DEBUG", 1: "INFO", 2: "DEFAULT", 3: "WARNING", 4: "ERROR", 5: "ERROR"}
# Core Data tables of the two SQLite log stores: Trio's AppLogStore, and LoopKit's PersistentDeviceLog (entity Entry)
STORE_TABLES = {
    "app": ("ZAPPLOGENTRY", "ZTIMESTAMP, ZLEVEL, ZCATEGORY, ZFILE, ZFUNCTION, ZLINE, ZMESSAGE, Z_PK"),
    "device": ("ZENTRY", "ZTIMESTAMP, ZTYPE, ZMANAGERIDENTIFIER, ZMESSAGE, Z_PK"),
}
LEVEL_PREFIX = re.compile(r"^\[(INFO|WARNING|ERROR)\] ")      # TandemKit's lines in the device log


# ---- reading logs by name ----

def read_logs(target: str, bundle_id: str, names: list[str]) -> dict[str, str | None]:
    """Each log's text, all its sources merged oldest first; None for a log the app has not written yet."""
    got = fetch(target, bundle_id, [r for n in names for r in SOURCES[n]])
    out = {n: _merge([got.get(r) for r in SOURCES[n]]) for n in names}
    if "tandem" in out and out["tandem"] is None:
        device = out["device"] if "device" in out else _merge(list(fetch(target, bundle_id, SOURCES["device"]).values()))
        out["tandem"] = tandem_from_device(device)
    return out


def _merge(texts: list[str | None]) -> str | None:
    if all(t is None for t in texts):
        return None
    return "\n".join(t.rstrip("\n") for t in texts if t)


def tandem_from_device(device: str | None) -> str | None:
    """TandemKit's entries in the device log's text."""
    lines = [e for _, _, e in _entries(device or "") if TANDEM_MANAGER.match(e)]
    return "\n".join(lines) if lines else None


def query(texts: dict[str, str | None], *, grep: str | None = None, ignore_case: bool = False,
          since: dt.datetime | None = None, level: str | None = None, lines: int | None = 200) -> list[str]:
    """Filtered entries of each log, oldest first; with several logs, each entry is prefixed with its log's name."""
    pat = re.compile(grep, re.I if ignore_case else 0) if grep else None
    minlvl = LEVELS[level] if level else None
    rows: list[tuple[dt.datetime | None, str, str]] = []
    for name, text in texts.items():
        for when, lvl, entry in _entries(text or ""):
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


def summary(texts: dict[str, str | None]) -> list[str]:
    """A line per log: how many entries, from when to when."""
    out = []
    for name, text in texts.items():
        if text is None:
            out.append(f"{name:7} not written yet")
            continue
        entries = _entries(text)
        when = [w for w, _, _ in entries if w]
        span = f", {when[0]:%Y-%m-%d %H:%M:%S} to {when[-1]:%Y-%m-%d %H:%M:%S}" if when else ""
        out.append(f"{name:7} {len(entries)} entries{span}")
    return out


def save(texts: dict[str, str | None], dest: Path) -> list[Path]:
    """Each log as text, <time>-<log>.log in dest."""
    dest.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%H%M%S")
    saved = []
    for name, text in texts.items():
        if text is not None:
            p = dest / f"{stamp}-{name}.log"
            p.write_text(text + "\n")
            saved.append(p)
    return saved


def tail(target: str, bundle_id: str, name: str, cursor: str | None) -> dict:
    """The log's entries after `cursor` (None: its last ones), for the web UI's live pane: {"text", "cursor", "more",
    "earlier", "reset": the log started over, "restart": read afresh from another source, "missing"}. The cursor is
    opaque to the caller: a source and a place in it."""
    at, _, pos = (cursor or "").rpartition("@")
    for rel, tandem_only in TAIL_SOURCES[name]:
        same = at == rel and pos.isdigit()
        got = _tail_source(target, bundle_id, rel, int(pos) if same else None)
        if got is None:
            continue
        text = got["text"]
        if tandem_only:
            text = "".join(e + "\n" for _, _, e in _entries(text) if TANDEM_MANAGER.match(e))
        return {"text": text, "cursor": f"{rel}@{got['offset']}", "more": got["offset"] < got["size"],
                "earlier": not same and got["earlier"], "reset": same and got["reset"],
                "restart": cursor is not None and not same, "missing": False}
    return {"text": "", "cursor": None, "more": False, "earlier": False, "reset": False, "restart": False,
            "missing": True}


def _tail_source(target: str, bundle_id: str, rel: str, offset: int | None) -> dict | None:
    if rel.endswith(".sqlite"):
        store = _local_store(target, rel, _container(target, bundle_id))
        return tail_app_log(store, offset) if store else None
    got = read_tail(target, bundle_id, rel, offset)
    if got["size"] is None:
        return None
    return {**got, "earlier": offset is None and got["offset"] - len(got["text"].encode()) > 0}


# ---- the sources ----

def _store_rows(path: Path, where: str, params: tuple, order: str = "ORDER BY ZTIMESTAMP, Z_PK") -> tuple[str, list[tuple]]:
    """("app" | "device", rows) of a SQLite log store."""
    import shutil
    import sqlite3
    import tempfile

    def rows_from(uri: str) -> tuple[str, list[tuple]]:
        con = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            kind = next((k for k, (t, _) in STORE_TABLES.items() if t in tables), None)
            if not kind:
                raise HarnessError(f"{path.name} is neither Trio's app log nor a device log")
            table, columns = STORE_TABLES[kind]
            return kind, con.execute(f"SELECT {columns} FROM {table} WHERE {where} {order}", params).fetchall()
        finally:
            con.close()

    try:      # in place and read-only: the app keeps writing it (WAL), and a reader that can write would checkpoint it
        return rows_from(f"file:{path}?mode=ro")
    except sqlite3.Error:
        # a reader that may not touch the -shm file: read a copy of the store and its WAL instead
        with tempfile.TemporaryDirectory() as tmp:
            for suffix in ("", "-wal", "-shm"):
                src = Path(f"{path}{suffix}")
                if src.exists():
                    shutil.copy2(src, Path(tmp) / f"store.sqlite{suffix}")
            return rows_from(f"file:{Path(tmp) / 'store.sqlite'}")


def _render_row(kind: str, row: tuple) -> str:
    when = dt.datetime.fromtimestamp((row[0] or 0) + CORE_DATA_EPOCH).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    if kind == "app":
        _, level, category, file, function, line, message, _pk = row
        return f"[{when} {SQLITE_LEVELS.get(level, 'INFO')}] [{category}] {file} - {function} - {line} - {message}"
    # the device identifier (a pump's serial, a sensor's) is left out
    _, entry_type, manager, message, _pk = row
    m = LEVEL_PREFIX.match(message or "")
    level = m.group(1) if m else "ERROR" if entry_type == "error" else "INFO"
    return f"[{when} {level}] [{manager}/{entry_type}] {(message or '')[m.end() if m else 0:]}"


def render_app_log(path: Path, days: float = SQLITE_DAYS) -> str:
    """A SQLite log store as text, one entry per row, in a format ENTRY parses. Trio's app log:
    [2026-11-07T14:02:49-0500 INFO] [Watch] AppleWatchManager.swift - handleBolusRequest - 829 - message
    A device log, [manager/entry type], the level from a TandemKit line's own [LEVEL] prefix:
    [2026-11-07T14:02:49-0500 INFO] [TandemPumpManager/delegate] TandemPumpManager.swift - enactTempBasal#1203: message"""
    import time
    since = time.time() - days * 86400 - CORE_DATA_EPOCH
    kind, rows = _store_rows(path, "ZTIMESTAMP >= ?", (since,))
    return "\n".join(_render_row(kind, r) for r in rows)


def tail_app_log(path: Path, after: int | None, limit: int = 500) -> dict:
    """The rows of a SQLite log store after row `after` (Z_PK; None: the last `limit`), at most `limit`: {"offset": the
    last row read, "size": the store's last row, "text", "reset": the store is behind `after` (a new one), "earlier":
    rows before the ones read were left out}."""
    _, last = _store_rows(path, "1", (), "ORDER BY Z_PK DESC LIMIT 1")
    size = last[0][-1] if last else 0
    reset = after is not None and after > size
    if after is None or reset:
        kind, rows = _store_rows(path, "1", (), f"ORDER BY Z_PK DESC LIMIT {int(limit)}")
        rows.reverse()
        earlier = bool(rows) and bool(_store_rows(path, "Z_PK < ?", (rows[0][-1],), "LIMIT 1")[1])
    else:
        kind, rows = _store_rows(path, "Z_PK > ?", (after,), f"ORDER BY Z_PK LIMIT {int(limit)}")
        earlier = False
    text = "".join(_render_row(kind, r) + "\n" for r in rows)
    return {"offset": rows[-1][-1] if rows else (0 if reset else after if after is not None else size), "size": size,
            "text": text, "reset": reset, "earlier": earlier}


_MIRRORED: dict[str, dict[str, tuple[str, str]]] = {}     # a store on the phone -> each file's (size, mtime) as copied
_MIRROR_DIR: Path | None = None


def _phone_store(container: str, rel: str) -> Path | None:
    """A copy here of a SQLite store on the phone, with its WAL (where the app's latest rows are), to read as the
    simulator's are. Copied again only when the store or its WAL changed, or was written to in the last few seconds
    (an mtime is to the second), so the live pane and `th oref`'s polling can afford it."""
    import tempfile
    from . import devices as D
    global _MIRROR_DIR
    p = f"{container}/Documents/{rel}"
    files = (p, p + "-wal")
    stat = '"$(stat -c "%s %Y" "$f" 2>/dev/null || stat -f "%z %m" "$f" 2>/dev/null || echo "? ?")"'
    probe = D.ssh_run("date +%s; for f in " + " ".join(shlex.quote(f) for f in files)
                      + f'; do [ -f "$f" ] && echo "$f {stat}"; done; true', check=False, timeout=30, mux=True)
    lines = probe.stdout.splitlines()
    if probe.returncode != 0 or not lines or not lines[0].strip().isdigit():
        raise HarnessError(f"could not look at {rel} on the phone over SSH: {(probe.stderr or probe.stdout)[-300:]}")
    now = int(lines[0])
    sigs = {f: (size, mtime) for f, size, mtime in (l.rsplit(" ", 2) for l in lines[1:] if l.count(" ") >= 2)}
    if p not in sigs:
        _MIRRORED.pop(p, None)
        return None
    if _MIRROR_DIR is None:
        _MIRROR_DIR = Path(tempfile.mkdtemp(prefix="th-phone-logs-"))
    local = _MIRROR_DIR / hashlib.sha1(p.encode()).hexdigest()[:12] / Path(rel).name
    local.parent.mkdir(exist_ok=True)
    had = _MIRRORED.get(p, {})

    def stale(f: str) -> bool:
        mtime = sigs[f][1]
        return had.get(f) != sigs[f] or not mtime.isdigit() or now - int(mtime) < 3

    pull = [f for f in files if f in sigs and stale(f)]
    if p in pull and files[1] in sigs and files[1] not in pull:
        pull.append(files[1])           # a store copied anew needs the WAL of the same moment
    if files[1] not in sigs:
        Path(f"{local}-wal").unlink(missing_ok=True)
    if pull:
        Path(f"{local}-shm").unlink(missing_ok=True)
        D.ssh_pull(pull, local.parent, mux=True)
        _MIRRORED[p] = {**had, **{f: sigs[f] for f in pull}}
    return local


def _local_store(target: str, rel: str, container: str) -> Path | None:
    """A SQLite store as a file here: in the simulator's container, or a copy of the phone's."""
    if target == "sim":
        f = Path(container) / "Documents" / rel
        return f if f.is_file() else None
    return _phone_store(container, rel)


def read_files(target: str, bundle_id: str, relpaths: list[str]) -> dict[str, str | None]:
    """relpath (under Documents/) -> its text, or None if the file does not exist; SQLite stores rendered as text. Runs
    where the device is reachable (this process, or the daemon)."""
    if target not in ("sim", "rooted-vnc"):
        raise HarnessError(f"th logs: target {target} is not supported (sim or rooted-vnc)")
    container = _container(target, bundle_id, fresh=True)
    out: dict[str, str | None] = {}
    for rel in [r for r in relpaths if r.endswith(".sqlite")]:
        store = _local_store(target, rel, container)
        out[rel] = render_app_log(store) if store else None
    texts = [r for r in relpaths if not r.endswith(".sqlite")]
    if not texts:
        return out
    if target == "sim":
        for rel in texts:
            f = Path(container) / "Documents" / rel
            out[rel] = f.read_text(errors="replace") if f.is_file() else None
        return out
    from . import devices as D
    script = "; ".join(
        f"echo {MARK}{shlex.quote(rel)}; p={shlex.quote(f'{container}/Documents/{rel}')}; "
        f"if [ -f \"$p\" ]; then cat \"$p\"; echo; else echo {MARK}-missing; fi" for rel in texts)
    cp = D.ssh_run(script, check=False, timeout=120)
    if MARK not in cp.stdout:
        raise HarnessError(f"reading the logs over SSH failed: {(cp.stderr or cp.stdout)[-500:] or 'no output'}")
    rel = None
    for chunk in re.split(rf"^{re.escape(MARK)}", cp.stdout, flags=re.M)[1:]:
        name, _, body = chunk.partition("\n")
        if name == "-missing":
            out[rel] = None
        else:
            rel = name
            out[rel] = body[:-1] if body.endswith("\n") else body     # the echo after cat
    return out


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


def _container(target: str, bundle_id: str, *, fresh: bool = False) -> str:
    """The app's data container on the target, cached for the live pane's polling; `fresh` looks it up again, since a
    reinstalled app gets a new one."""
    key = (target, bundle_id)
    if fresh or key not in _CONTAINERS:
        if target == "sim":
            from . import xcode
            cp = xcode.xcrun(["simctl", "get_app_container", xcode.ensure_simulator()["udid"], bundle_id, "data"],
                             quiet=True, check=False)
            if cp.returncode != 0:
                raise HarnessError(f"{bundle_id} is not installed on the harness simulator")
            _CONTAINERS[key] = cp.stdout.strip()
        else:
            from . import devices as D
            _CONTAINERS[key] = D.ssh_app_container(bundle_id)
    return _CONTAINERS[key]


def read_tail(target: str, bundle_id: str, rel: str, offset: int | None, max_bytes: int = 256 * 1024) -> dict:
    """Bytes of the text file Documents/<rel> after `offset` (None: the last 64 KB), at most max_bytes: {"offset",
    "size", "text", "reset"}. reset=True when the file shrank (rotated at midnight) and reading restarted from the top."""
    if target == "sim":
        f = Path(_container(target, bundle_id)) / "Documents" / rel
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
    p = shlex.quote(f"{_container(target, bundle_id)}/Documents/{rel}")
    # size first, then the bytes: `tail -c +N` is 1-based; BSD head/tail on the phone
    probe = D.ssh_run(f"if [ -f {p} ]; then wc -c < {p}; else echo missing; fi", check=False, timeout=30, mux=True)
    out = probe.stdout.strip()
    if out == "missing":
        return {"offset": 0, "size": None, "text": "", "reset": False}
    if probe.returncode != 0 or not out.isdigit():
        _CONTAINERS.pop((target, bundle_id), None)          # reinstalled apps get a new container
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
