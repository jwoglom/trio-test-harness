"""Locate Xcode, expose DEVELOPER_DIR, and wrap simctl / devicectl."""
from __future__ import annotations

import glob
import json
import os
import plistlib
from pathlib import Path

from .config import CONFIG, REPO_ROOT
from .util import HarnessError, log, run

# The iPhone simulator in use when it is not TH_SIM_NAME: the phone of a phone + watch pair (harness/watch.py).
SIM_CURRENT_FILE = REPO_ROOT / "runs" / ".tmp" / "sim-udid.txt"


def find_xcode() -> Path | None:
    """Return the Xcode.app to use (config override, else newest /Applications/Xcode*.app)."""
    if CONFIG.get("TH_XCODE_APP"):
        p = Path(CONFIG.get("TH_XCODE_APP")).expanduser()
        return p if p.exists() else None
    if os.environ.get("DEVELOPER_DIR"):
        p = Path(os.environ["DEVELOPER_DIR"]).parent.parent
        if p.exists():
            return p
    cands = glob.glob("/Applications/Xcode*.app") + glob.glob(str(Path.home() / "Applications/Xcode*.app"))
    best, best_ver = None, ()
    for c in cands:
        try:
            info = plistlib.loads((Path(c) / "Contents/Info.plist").read_bytes())
            ver = tuple(int(x) for x in info.get("CFBundleShortVersionString", "0").split("."))
        except Exception:
            ver = ()
        if ver >= best_ver:
            best, best_ver = Path(c), ver
    return best


def xcode_version(app: Path) -> str:
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    return info.get("CFBundleShortVersionString", "?")


def developer_env() -> dict[str, str]:
    """Environment that makes xcrun/xcodebuild use our Xcode without sudo xcode-select."""
    app = find_xcode()
    if not app:
        raise HarnessError(
            "No Xcode.app found. Run ./bootstrap-xcode.sh (Xcode 26.3 is the newest version that runs on macOS 15.x)."
        )
    return {"DEVELOPER_DIR": str(app / "Contents/Developer")}


def xcrun(args: list[str], **kw):
    return run(["xcrun", *args], env=developer_env(), **kw)


# ---------------- simctl -----------------

def simctl_json(args: list[str]) -> dict:
    cp = xcrun(["simctl", *args, "--json"] if args[0] == "list" else ["simctl", *args], quiet=True)
    return json.loads(cp.stdout)


def list_runtimes(platform: str = "iOS") -> list[dict]:
    data = simctl_json(["list", "runtimes"])
    return [r for r in data.get("runtimes", []) if r.get("platform") == platform and r.get("isAvailable")]


def list_devicetypes() -> list[dict]:
    return simctl_json(["list", "devicetypes"]).get("devicetypes", [])


def list_devices() -> list[dict]:
    data = simctl_json(["list", "devices"])
    out = []
    for runtime_id, devs in data.get("devices", {}).items():
        for d in devs:
            d = dict(d)
            d["runtimeIdentifier"] = runtime_id
            out.append(d)
    return out


def pick_runtime(name: str = "", platform: str = "iOS") -> dict:
    rts = list_runtimes(platform)
    if not rts:
        flag = "" if platform == "iOS" else f" --platform {platform}"
        raise HarnessError(f"No {platform} simulator runtime installed. Run: th sim install-runtime{flag}")
    if name:
        for r in rts:
            if r["name"] == name or r["version"] == name.replace(platform, "").strip():
                return r
        raise HarnessError(f"runtime {name!r} not found; have {[r['name'] for r in rts]}")
    return sorted(rts, key=lambda r: tuple(int(x) for x in r["version"].split(".")))[-1]


def pick_devicetype(name: str) -> dict:
    for dt in list_devicetypes():
        if dt["name"] == name:
            return dt
    raise HarnessError(f"device type {name!r} not found; try: xcrun simctl list devicetypes")


def ensure_simulator() -> dict:
    """The harness's iPhone simulator, created on first use: the phone of the watch pair in use (`th watch pair`), else
    TH_SIM_NAME. Returns its simctl record."""
    if SIM_CURRENT_FILE.exists():
        udid = SIM_CURRENT_FILE.read_text().strip()
        for d in list_devices():
            if d["udid"] == udid and d.get("isAvailable", True):
                return d
    name = CONFIG.get("TH_SIM_NAME")
    rt = pick_runtime(CONFIG.get("TH_SIM_RUNTIME"))
    for d in list_devices():
        if d["name"] == name and d["runtimeIdentifier"] == rt["identifier"] and d.get("isAvailable", True):
            return d
    dt = pick_devicetype(CONFIG.get("TH_SIM_DEVICE"))
    udid = xcrun(["simctl", "create", name, dt["identifier"], rt["identifier"]]).stdout.strip()
    for d in list_devices():
        if d["udid"] == udid:
            return d
    raise HarnessError("simulator creation failed")


def create_device(name: str, devicetype: str, runtime: dict) -> dict:
    udid = xcrun(["simctl", "create", name, pick_devicetype(devicetype)["identifier"], runtime["identifier"]]).stdout.strip()
    for d in list_devices():
        if d["udid"] == udid:
            return d
    raise HarnessError(f"simulator creation failed: {name}")


def list_pairs() -> dict:
    """pair id -> {"watch": {...}, "phone": {...}, "state": "(active, connected)"}"""
    return simctl_json(["list", "pairs"]).get("pairs", {})


def watch_devicetypes() -> list[dict]:
    """The watch models the watch runtime in use can run (name, identifier), newest first as simctl lists them."""
    rt = pick_runtime(CONFIG.get("TH_WATCH_SIM_RUNTIME"), "watchOS")
    ver = _version(rt["version"])
    return [dt for dt in list_devicetypes() if dt.get("productFamily") == "Apple Watch"
            and _version(dt.get("minRuntimeVersionString", "0")) <= ver <= _version(dt.get("maxRuntimeVersionString", "65535"))]


def _version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def boot_simulator(udid: str) -> None:
    devices = list_devices()
    me = next((d for d in devices if d["udid"] == udid), {})
    state = me.get("state")
    if state != "Booted":
        # One harness iPhone and one harness watch at a time: four simulators on a small Mac fail to boot, and a second
        # pair's phone answers nothing the agent drives. Simulators outside the harness are left alone.
        family = me.get("runtimeIdentifier", "").split(".")[-1].split("-")[0]
        names = (CONFIG.get("TH_SIM_NAME"), CONFIG.get("TH_WATCH_SIM_NAME"))
        for d in devices:
            if d["udid"] != udid and d["state"] == "Booted" and d["name"].startswith(names) \
                    and d["runtimeIdentifier"].split(".")[-1].split("-")[0] == family:
                log(f"shutting down {d['name']}: one harness {family} simulator at a time")
                xcrun(["simctl", "shutdown", d["udid"]], check=False, quiet=True)
        # "launchd failed to respond" while the Mac is loaded (a build, another simulator's first boot); a later try boots
        import time
        for attempt in range(4):
            cp = xcrun(["simctl", "boot", udid], check=attempt == 3)
            if cp.returncode == 0 or "current state: Booted" in (cp.stderr or ""):
                break
            log(f"simulator {udid} did not boot ({(cp.stderr or cp.stdout).strip().splitlines()[-1:]}); retrying in 60 s")
            time.sleep(60)
    xcrun(["simctl", "bootstatus", udid, "-b"], timeout=900)
    # Bring the Simulator.app UI up so screenshots include a live window (also needed by idb).
    app = find_xcode()
    run(["open", "-a", str(app / "Contents/Developer/Applications/Simulator.app"), "--args", "-CurrentDeviceUDID", udid],
        check=False, quiet=True)


def app_container(udid: str, bundle_id: str, kind: str = "data") -> Path:
    cp = xcrun(["simctl", "get_app_container", udid, bundle_id, kind], quiet=True)
    return Path(cp.stdout.strip())


# ---------------- devicectl (physical devices, needs Xcode) -----------------

def devicectl_json(args: list[str]) -> dict:
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        out = tf.name
    xcrun(["devicectl", *args, "--json-output", out], quiet=True)
    return json.loads(Path(out).read_text())


def devicectl_devices() -> list[dict]:
    try:
        data = devicectl_json(["list", "devices"])
    except HarnessError:
        return []
    return data.get("result", {}).get("devices", [])
