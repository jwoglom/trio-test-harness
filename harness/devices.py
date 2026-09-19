"""Physical-iPhone utilities: usbmux discovery (pymobiledevice3), TrollStore IPA
serving, and devicectl-based install/launch for properly signed builds."""
from __future__ import annotations

import http.server
import os
import shutil
import socketserver
import threading
from pathlib import Path

from .config import CONFIG
from .util import HarnessError, lan_ip, log, run, which
from . import xcode


def usbmux_devices() -> list[dict]:
    """Devices visible to usbmuxd (USB or Wi-Fi paired), via the pymobiledevice3 CLI."""
    exe = which("pymobiledevice3")
    if not exe:
        raise HarnessError("pymobiledevice3 not installed in .venv")
    cp = run([exe, "usbmux", "list"], check=False, quiet=True, timeout=30)
    if cp.returncode != 0:
        raise HarnessError(f"pymobiledevice3 usbmux list failed: {cp.stderr[-300:]}")
    import json as _json
    try:
        raw = _json.loads(cp.stdout or "[]")
    except _json.JSONDecodeError:
        raw = []
    out = []
    for d in raw:
        out.append({
            "udid": d.get("UniqueDeviceID") or d.get("Identifier"),
            "connection": d.get("ConnectionType", "?"),
            "name": d.get("DeviceName"),
            "ios": d.get("ProductVersion"),
            "model": d.get("ProductType"),
            "paired": True,
        })
    return out


def pick_udid() -> str:
    if CONFIG.get("TH_DEVICE_UDID"):
        return CONFIG.get("TH_DEVICE_UDID")
    devs = [d for d in usbmux_devices() if d["connection"] == "USB"] or usbmux_devices()
    if not devs:
        raise HarnessError("no iPhone found on usbmuxd (plug it in over USB, unlock it, and trust this Mac)")
    if len(devs) > 1:
        raise HarnessError(f"several devices connected; set TH_DEVICE_UDID. Found: {[d['udid'] for d in devs]}")
    return devs[0]["udid"]


# ---------------- TrollStore install path -----------------

class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: D401
        log("http " + (fmt % args))


def serve_dir(directory: Path, port: int) -> socketserver.TCPServer:
    handler = lambda *a, **k: _Quiet(*a, directory=str(directory), **k)  # noqa: E731
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("0.0.0.0", port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def trollstore_install_url(ipa: Path) -> tuple[str, socketserver.TCPServer]:
    """Serve the IPA over HTTP and return the TrollStore URL-scheme link the phone must open.

    TrollStore registers apple-magnifier://install?url=<ipa url>. Open it in Safari on the phone
    (the harness can type it via VNC) and confirm the install prompt.
    """
    ipa = ipa.resolve()
    port = int(CONFIG.get("TH_IPA_HTTP_PORT"))
    host = CONFIG.get("TH_IPA_HTTP_HOST") or lan_ip()
    httpd = serve_dir(ipa.parent, port)
    ipa_url = f"http://{host}:{port}/{ipa.name}"
    return f"apple-magnifier://install?url={ipa_url}", httpd


# ---------------- AppSync Unified path (jailbroken phone, unsigned IPA) -----------------

def appsync_install(ipa: Path, udid: str | None = None) -> None:
    """Install an (ad-hoc signed) IPA through installd; works once AppSync Unified is on the phone."""
    exe = which("pymobiledevice3")
    if not exe:
        raise HarnessError("pymobiledevice3 missing in .venv")
    cmd = [exe, "apps", "install", str(ipa)]
    if udid:
        cmd += ["--udid", udid]
    run(cmd, timeout=900, capture=False)


# ---------------- devicectl path (signed builds) -----------------

def devicectl_install(udid: str, app_or_ipa: Path) -> None:
    xcode.xcrun(["devicectl", "device", "install", "app", "--device", udid, str(app_or_ipa)], timeout=600)


def devicectl_launch(udid: str, bundle_id: str, *args: str) -> None:
    xcode.xcrun(["devicectl", "device", "process", "launch", "--terminate-existing", "--device", udid, bundle_id, *args])


def devicectl_open_url(udid: str, url: str) -> None:
    """Open a URL on the device by launching Safari with -u (works for custom schemes too)."""
    devicectl_launch(udid, "com.apple.mobilesafari", "-u", url)
