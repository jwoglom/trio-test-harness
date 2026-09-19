"""Locate Xcode, expose DEVELOPER_DIR, and wrap simctl / devicectl."""
from __future__ import annotations

import glob
import json
import os
import plistlib
from pathlib import Path

from .config import CONFIG
from .util import HarnessError, run


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


def pick_runtime(name: str = "") -> dict:
    rts = list_runtimes()
    if not rts:
        raise HarnessError("No iOS simulator runtime installed. Run: th sim install-runtime")
    if name:
        for r in rts:
            if r["name"] == name or r["version"] == name.replace("iOS", "").strip():
                return r
        raise HarnessError(f"runtime {name!r} not found; have {[r['name'] for r in rts]}")
    return sorted(rts, key=lambda r: tuple(int(x) for x in r["version"].split(".")))[-1]


def pick_devicetype(name: str) -> dict:
    for dt in list_devicetypes():
        if dt["name"] == name:
            return dt
    raise HarnessError(f"device type {name!r} not found; try: xcrun simctl list devicetypes")


def ensure_simulator() -> dict:
    """Find or create the dedicated harness simulator; return its simctl record."""
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


def boot_simulator(udid: str) -> None:
    state = {d["udid"]: d["state"] for d in list_devices()}.get(udid)
    if state != "Booted":
        xcrun(["simctl", "boot", udid])
    xcrun(["simctl", "bootstatus", udid, "-b"], timeout=300)
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
