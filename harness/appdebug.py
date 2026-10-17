"""Debugging what the app did, beyond its screens, its own log files (`th logs`) and its database (`th db`):

    th oslog --since 10m --grep bolus -i      # the system log of the app's process: the app, TandemKit, LoopKit, ...
    th oslog --enable-debug                   # simulator: keep the app's and TandemKit's debug messages from now on
    th oslog --stream 60 --level debug        # simulator: record the next 60 s live, debug messages included
    th crashes                                # the app's crash reports since the run started (else --since 1d)
    th crashes --save                         # copy them, identifiers removed, into the run's crashes/; one step each
    th files ls [monitor]                     # the files under the app's Documents/ (Trio: OpenAPS JSON state)
    th files cat monitor/iob.json             # print one (property lists as JSON)
    th perf                                   # CPU and memory of the running app
    th perf --watch 120 --interval 5 --save   # sample it for 2 min into the run's perf.csv
    th perf --leaks                           # leaked allocations (simulator)

System log: `log show` over the simulator's log store, or on the phone a .logarchive collected over USB
(pymobiledevice3 syslog collect) and read with the Mac's `log`. Info messages are kept only while recent and debug ones
not at all unless --enable-debug configured the subsystem (simulator; --disable-debug undoes it), or --stream records.
Crash reports: the simulator's go to this Mac's ~/Library/Logs/DiagnosticReports (filtered to this app on the harness
simulator); the phone's are pulled over USB (pymobiledevice3 crash pull). Process stats: ps / footprint / leaks on the
simulator app's process (it runs on the Mac); ps over SSH on the phone.

Everything runs where the device is reachable: in this process, or in the target's daemon when one runs, which is all
the sandboxed web UI/CI agent can reach (call()). Phone output can hold real data: keep it out of reports and PRs.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from .util import HarnessError, run, which

# Functions the daemon runs for a client: call(target, name, ...) -> name(target, ...)
REMOTE = {"oslog_entries", "oslog_stream", "oslog_config", "crash_reports", "list_files", "read_file", "perf_sample",
          "leaks"}
LEVELS = ("default", "info", "debug")          # what `log show` includes: default and above, + info, + debug
TYPES = {"Fault": "F", "Error": "E", "Default": "N", "Info": "I", "Debug": "D"}
DEBUG_SUBSYSTEMS = {"trio": ("{bundle}", "com.jwoglom.TandemKit"),
                    "loop": ("{bundle}", "com.loopkit.LoopKit", "com.jwoglom.TandemKit")}
MAX_ENTRIES = 20000


def call(target: str, name: str, *args, **kwargs):
    """name(target, *args) in the target's daemon when one runs (the sandboxed agent can reach only that), else here."""
    from . import daemon as Dm
    if target not in ("sim", "rooted-vnc"):
        raise HarnessError(f"target {target} is not supported here (sim or rooted-vnc)")
    if Dm.is_running(target):
        try:
            return Dm.RemoteDriver(target, timeout=900)._call(name, *args, **kwargs)
        except HarnessError as e:
            if name in str(e) and "AttributeError" in str(e):
                raise HarnessError(f"the running {target} daemon predates this command; restart it: "
                                   f"th --target {target} daemon stop, then daemon start") from None
            raise
    return globals()[name](target, *args, **kwargs)


def _udid() -> str:
    from . import xcode
    return xcode.ensure_simulator()["udid"]


def _simctl(args: list[str], **kw):
    from . import xcode
    return xcode.xcrun(["simctl", *args], **kw)


# ---------------- system log -----------------

def _predicate(process: str, subsystem: str | None, apple: bool = False) -> str:
    """The app's process; one subsystem; Apple's frameworks (UIKit background tasks, ...) left out unless `apple`."""
    p = f'process == "{process}"'
    if subsystem:
        return p + f' AND subsystem BEGINSWITH "{subsystem}"'
    return p if apple else p + ' AND NOT subsystem BEGINSWITH "com.apple."'


def _level_flags(level: str) -> list[str]:
    return {"default": [], "info": ["--info"], "debug": ["--info", "--debug"]}[level]


def _parse_ndjson(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("eventType", "logEvent") != "logEvent" or "eventMessage" not in e:
            continue            # activities and signposts (Security's SecItem... calls), not messages
        out.append({"t": e.get("timestamp", ""), "type": TYPES.get(e.get("messageType"), "?"),
                    "subsystem": e.get("subsystem", ""), "category": e.get("category", ""),
                    "msg": e.get("eventMessage", "")})
    return out[-MAX_ENTRIES:]


def oslog_entries(target: str, process: str, start: str, level: str = "default", subsystem: str | None = None,
                  apple: bool = False) -> list[dict]:
    """System log entries of `process` since `start` (local time, "YYYY-MM-DD HH:MM:SS")."""
    show = ["--style", "ndjson", "--start", start, *_level_flags(level), "--predicate",
            _predicate(process, subsystem, apple)]
    if target == "sim":
        cp = _simctl(["spawn", _udid(), "log", "show", *show], quiet=True, check=False, timeout=600)
        if cp.returncode != 0 and not cp.stdout:
            raise HarnessError(f"log show failed: {cp.stderr[-500:]}")
        return _parse_ndjson(cp.stdout)
    exe = which("pymobiledevice3")
    if not exe:
        raise HarnessError("pymobiledevice3 missing in .venv")
    from . import devices as D
    from .config import CONFIG
    t0 = int(dt.datetime.strptime(start, "%Y-%m-%d %H:%M:%S").timestamp())
    with tempfile.TemporaryDirectory(prefix="th-oslog-") as tmp:
        archive = Path(tmp) / "phone.logarchive"
        cmd = [exe, "syslog", "collect", str(archive), "--start-time", str(t0)]
        if CONFIG.get("TH_DEVICE_UDID") or len(D.usbmux_devices()) > 1:
            cmd += ["--udid", D.pick_udid()]
        cp = run(cmd, check=False, quiet=True, timeout=900)
        if cp.returncode != 0 or not archive.exists():
            raise HarnessError(f"collecting the phone's log failed: {(cp.stderr or cp.stdout)[-500:]}")
        cp = run(["log", "show", str(archive), *show], check=False, quiet=True, timeout=600)
        return _parse_ndjson(cp.stdout)


def oslog_stream(target: str, process: str, seconds: float, level: str = "debug", subsystem: str | None = None,
                 apple: bool = False) -> list[dict]:
    """Record `seconds` of the live log (simulator): debug messages arrive here even when the store does not keep them."""
    if target != "sim":
        raise HarnessError("--stream works on the simulator; on the phone use oslog --since")
    # log's own --timeout ends it: killing xcrun would leave the log process inside the simulator running
    cp = _simctl(["spawn", _udid(), "log", "stream", "--style", "ndjson", "--level", level, "--timeout", f"{int(seconds)}s",
                  "--predicate", _predicate(process, subsystem, apple)], quiet=True, check=False, timeout=seconds + 120)
    out = cp.stdout
    return _parse_ndjson(out)


def oslog_config(target: str, subsystems: list[str], enable: bool) -> str:
    """Make the simulator keep (or stop keeping) debug messages of these subsystems."""
    if target != "sim":
        raise HarnessError("--enable-debug / --disable-debug work on the simulator only")
    mode = "level:debug,persist:debug" if enable else "level:default,persist:default"
    for s in subsystems:
        _simctl(["spawn", _udid(), "log", "config", "--subsystem", s, "--mode", mode], quiet=True)
    return mode


def format_entry(e: dict) -> str:
    where = "/".join(x for x in (e["subsystem"].rsplit(".", 1)[-1], e["category"]) if x)
    return f"{e['t'][:23]} {e['type']} {where}: {e['msg']}"


# ---------------- crash reports -----------------

# Identifiers in .ips reports that say which Mac or phone, not what crashed
DROP_KEYS = {"crashReporterKey", "sleepWakeUUID", "incident", "incident_id", "userID", "deviceIdentifierForVendor"}
UUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"


def _redact_path(s: str) -> str:
    """Home folders, and UUIDs inside longer strings (the simulator's, containers'). A value that is only a UUID stays:
    those are the binaries' build UUIDs, which symbolication needs."""
    s = re.sub(r"/Users/[^/]+", "~", s)
    return s if re.fullmatch(UUID, s) else re.sub(UUID, "<uuid>", s)


def _redact(obj):
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items() if k not in DROP_KEYS}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return _redact_path(obj) if isinstance(obj, str) else obj


def parse_ips(text: str) -> tuple[dict, dict]:
    """An .ips report: (header, body). Both are JSON; the header is the first line."""
    head, _, rest = text.partition("\n")
    try:
        return json.loads(head), (json.loads(rest) if rest.strip() else {})
    except json.JSONDecodeError:
        return {}, {}


def crash_reports(target: str, process: str, bundle_id: str, since: float) -> list[dict]:
    """The app's crash reports newer than `since` (epoch seconds): [{"name", "time", "text"}], identifiers removed."""
    found: list[tuple[str, str]] = []
    if target == "sim":
        udid = _udid()
        root = Path.home() / "Library" / "Logs" / "DiagnosticReports"
        for f in sorted(root.glob("**/*.ips")):
            if f.stat().st_mtime < since or not f.name.startswith(process):
                continue
            text = f.read_text(errors="replace")
            head, body = parse_ips(text)
            if (head.get("bundleID") or body.get("bundleInfo", {}).get("CFBundleIdentifier")) == bundle_id \
                    and udid in body.get("procPath", ""):
                found.append((f.name, text))
    else:
        exe = which("pymobiledevice3")
        if not exe:
            raise HarnessError("pymobiledevice3 missing in .venv")
        from . import devices as D
        from .config import CONFIG
        with tempfile.TemporaryDirectory(prefix="th-crashes-") as tmp:
            cmd = [exe, "crash", "pull", tmp, "--match", process]
            if CONFIG.get("TH_DEVICE_UDID") or len(D.usbmux_devices()) > 1:
                cmd += ["--udid", D.pick_udid()]
            cp = run(cmd, check=False, quiet=True, timeout=600)
            if cp.returncode != 0:
                raise HarnessError(f"pulling the phone's crash reports failed: {(cp.stderr or cp.stdout)[-500:]}")
            for f in sorted(Path(tmp).glob("**/*.ips")):
                text = f.read_text(errors="replace")
                head, _ = parse_ips(text)
                if head.get("bundleID") == bundle_id and _ips_time(head) >= since:
                    found.append((f.name, text))
    out = []
    for name, text in found:
        head, body = parse_ips(text)
        clean = json.dumps(_redact(head)) + "\n" + json.dumps(_redact(body), indent=2) if head else _redact_path(text)
        out.append({"name": _redact_path(name), "time": _ips_time(head), "text": clean})
    return sorted(out, key=lambda c: c["time"])


def _ips_time(head: dict) -> float:
    try:
        return dt.datetime.strptime(head.get("timestamp", ""), "%Y-%m-%d %H:%M:%S.%f %z").timestamp()
    except ValueError:
        return 0.0


def summarize_crash(text: str, frames: int = 12) -> str:
    """Exception, termination reason and the crashed thread's top frames."""
    head, body = parse_ips(text)
    if not body:
        return text[:2000]
    ex, term = body.get("exception", {}), body.get("termination", {})
    lines = [f"{head.get('app_name', '?')} {head.get('app_version', '')} ({head.get('build_version', '')}) crashed "
             f"{body.get('captureTime', head.get('timestamp', '?'))[:19]}: "
             + " ".join(str(x) for x in (ex.get("type"), f"({ex.get('signal')})" if ex.get("signal") else "",
                                         ex.get("subtype") or "") if x)]
    if term:
        lines.append(f"  termination: {term.get('namespace', '')} {term.get('code', '')} {term.get('indicator', '')}"
                     + (f" by {term['byProc']}" if term.get("byProc") else ""))
        lines += [f"    {r}" for r in term.get("reasons", [])[:5]]
    asi = body.get("asi") or {}
    for proc, msgs in asi.items():
        lines += [f"  {proc}: {m}" for m in msgs[:5]]
    images = body.get("usedImages", [])
    threads = body.get("threads", [])
    idx = body.get("faultingThread", 0)
    if idx < len(threads):
        th = threads[idx]
        lines.append(f"  crashed thread {idx}" + (f" ({th.get('queue') or th.get('name')})" if th.get("queue") or th.get("name") else "") + ":")
        for i, fr in enumerate(th.get("frames", [])[:frames]):
            img = images[fr["imageIndex"]] if fr.get("imageIndex", -1) < len(images) else {}
            sym = fr.get("symbol") or f"+{fr.get('imageOffset', '?')}"
            src = f"  ({fr['sourceFile']}:{fr.get('sourceLine', '?')})" if fr.get("sourceFile") else ""
            lines.append(f"    {i:2} {img.get('name', '?'):28} {sym}{src}")
    return "\n".join(lines)


# ---------------- Documents files -----------------

def _sim_docs(bundle_id: str) -> Path:
    cp = _simctl(["get_app_container", _udid(), bundle_id, "data"], quiet=True, check=False)
    if cp.returncode != 0:
        raise HarnessError(f"{bundle_id} is not installed on the harness simulator")
    return Path(cp.stdout.strip()) / "Documents"


def _safe_rel(rel: str) -> str:
    rel = rel.strip().lstrip("/")
    if ".." in Path(rel).parts:
        raise HarnessError(f"{rel}: paths stay inside the app's Documents/")
    return rel


def list_files(target: str, bundle_id: str, rel: str = "") -> list[dict]:
    """Files under Documents/<rel>: [{"path", "size", "mtime" (simulator only)}]."""
    rel = _safe_rel(rel)
    if target == "sim":
        root = _sim_docs(bundle_id)
        base = root / rel
        if base.is_file():
            return [{"path": rel, "size": base.stat().st_size, "mtime": base.stat().st_mtime}]
        return [{"path": str(f.relative_to(root)), "size": f.stat().st_size, "mtime": f.stat().st_mtime}
                for f in sorted(base.rglob("*")) if f.is_file()]
    from . import devices as D
    docs = f"{D.ssh_app_container(bundle_id)}/Documents"
    cp = D.ssh_run(f"cd {shlex.quote(docs)} && find ./{shlex.quote(rel)} -type f -exec wc -c {{}} +", check=False, timeout=60)
    out = []
    for line in cp.stdout.splitlines():
        size, _, path = line.strip().partition(" ")
        if size.isdigit() and path and path != "total":
            out.append({"path": path.removeprefix("./").replace("//", "/"), "size": int(size), "mtime": None})
    return sorted(out, key=lambda f: f["path"])


def read_file(target: str, bundle_id: str, rel: str, max_bytes: int = 8 * 1024 * 1024) -> str:
    """Documents/<rel>, base64-encoded (property lists are often binary)."""
    rel = _safe_rel(rel)
    if target == "sim":
        f = _sim_docs(bundle_id) / rel
        if not f.is_file():
            raise HarnessError(f"no file Documents/{rel} (`th files ls`)")
        return base64.b64encode(f.read_bytes()[:max_bytes]).decode()
    from . import devices as D
    p = shlex.quote(f"{D.ssh_app_container(bundle_id)}/Documents/{rel}")
    cp = D.ssh_run(f"if [ -f {p} ]; then head -c {max_bytes} {p} | base64; else echo missing; fi", check=False, timeout=120)
    if cp.stdout.strip() == "missing":
        raise HarnessError(f"no file Documents/{rel} (`th files ls`)")
    if cp.returncode != 0:
        raise HarnessError(f"reading {rel} over SSH failed: {(cp.stderr or cp.stdout)[-300:]}")
    return "".join(cp.stdout.split())


def decode_file(rel: str, b64: str) -> str:
    data = base64.b64decode(b64)
    if rel.endswith(".plist"):
        import plistlib
        try:
            return json.dumps(plistlib.loads(data), indent=2, default=str)
        except Exception:  # noqa: BLE001 - not a property list after all
            pass
    return data.decode(errors="replace")


# ---------------- CPU / memory -----------------

def _sim_pid(bundle_id: str) -> int:
    cp = _simctl(["spawn", _udid(), "launchctl", "list"], quiet=True)
    for line in cp.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and f"UIKitApplication:{bundle_id}[" in parts[2] and parts[0].isdigit():
            return int(parts[0])
    raise HarnessError(f"{bundle_id} is not running on the simulator (`th launch`)")


def _mb(text: str) -> float | None:
    """'123.4 MB' / '512 KB' / '1.2 GB' -> MB."""
    m = re.search(r"([\d.]+)\s*([KMG])B", text)
    return round(float(m.group(1)) * {"K": 1 / 1024, "M": 1, "G": 1024}[m.group(2)], 1) if m else None


def perf_sample(target: str, process: str, bundle_id: str) -> dict:
    """One sample: pid, cpu % (of one core), resident and physical-footprint memory (MB), threads, CPU time."""
    if target == "sim":
        pid = _sim_pid(bundle_id)
        cp = run(["ps", "-o", "%cpu=,rss=,time=", "-p", str(pid)], quiet=True, check=False)
        cpu, rss, cputime = (cp.stdout.split() + ["", "", ""])[:3]
        thr = run(["ps", "-M", "-p", str(pid)], quiet=True, check=False).stdout.count("\n") - 1
        fp = run(["footprint", str(pid)], quiet=True, check=False).stdout
        m = re.search(r"(?:phys_footprint|Footprint):\s*([\d.]+\s*[KMG]B)", fp)
        return {"t": time.time(), "pid": pid, "cpu": float(cpu or 0), "rss_mb": round(int(rss or 0) / 1024, 1),
                "footprint_mb": _mb(m.group(1)) if m else None, "threads": max(thr, 0), "cpu_time": cputime}
    from . import devices as D
    cp = D.ssh_run("ps -A -o pid=,%cpu=,rss=,time=,comm=", check=False, timeout=30)
    for line in cp.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[4].endswith(f"/{process}.app/{process}"):
            return {"t": time.time(), "pid": int(parts[0]), "cpu": float(parts[1]), "rss_mb": round(int(parts[2]) / 1024, 1),
                    "footprint_mb": None, "threads": None, "cpu_time": parts[3]}
    if cp.returncode != 0:
        raise HarnessError(f"ps over SSH failed: {(cp.stderr or cp.stdout)[-300:]}")
    raise HarnessError(f"{process} is not running on the phone")


def leaks(target: str, bundle_id: str, timeout: float = 900) -> dict:
    """`leaks` on the simulator app: {"summary", "leaks", "bytes", "types": [[count, bytes, type]...], "restricted"}.
    It is slow (minutes on a small Mac) and keeps going after its summary, so the summary ends it. A build without the
    get-task-allow entitlement is "not debuggable": leaks then reads only its read-only memory, and its count is a floor."""
    if target != "sim":
        raise HarnessError("--leaks works on the simulator only")
    pid = _sim_pid(bundle_id)
    import threading
    p = subprocess.Popen(["leaks", str(pid)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    watchdog = threading.Timer(timeout, p.kill)
    watchdog.start()
    lines: list[str] = []
    m = None
    try:
        for line in p.stdout:
            lines.append(line)
            m = m or re.search(r"Process \d+: (\d+) leaks? for (\d+) total leaked bytes", line)
            # after the summary come the leaked objects, if any, ending at a blank line
            if m and (int(m.group(1)) == 0 or not line.strip() and any(x.startswith(("Leak", "ROOT")) for x in lines[-3:-1])):
                break
    finally:
        watchdog.cancel()
        p.kill()
        p.wait()
    out = "".join(lines)
    if not m:
        raise HarnessError(f"leaks gave no summary within {timeout:.0f}s: {out[-500:]}")
    types: dict[str, list[int]] = {}
    for lm in re.finditer(r"^\s*(?:ROOT LEAK|Leak): .*?size=(\d+).*?\s(\S+)\s*$", out, re.M):
        t = types.setdefault(lm.group(2), [0, 0])
        t[0] += 1; t[1] += int(lm.group(1))
    top = sorted(([c, b, n] for n, (c, b) in types.items()), reverse=True)[:25]
    return {"summary": m.group(0), "leaks": int(m.group(1)), "bytes": int(m.group(2)), "types": top,
            "restricted": "is not debuggable" in out}


# ---------------- client side: runs -----------------

def run_started(run) -> float | None:
    created = (run.meta or {}).get("created")
    try:
        return dt.datetime.fromisoformat(created).astimezone().timestamp() if created else None
    except ValueError:
        return None


def save_crashes(run, target: str, process: str, bundle_id: str, since: float | None = None) -> list[dict]:
    """Copy the app's crash reports since the run started into the run's crashes/ (identifiers removed), with a run
    step for each new one. Returns the new ones."""
    since = since if since is not None else (run_started(run) or time.time() - 86400)
    new = []
    dest = run.path / "crashes"
    for r in call(target, "crash_reports", process, bundle_id, since):
        f = dest / r["name"]
        if f.exists():
            continue
        dest.mkdir(parents=True, exist_ok=True)
        f.write_text(r["text"])
        run.log_step("crash", summarize_crash(r["text"], frames=0).splitlines()[0], file=f"crashes/{r['name']}")
        new.append(r)
    return new
