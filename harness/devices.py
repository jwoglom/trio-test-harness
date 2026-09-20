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


# ---------------- SSH to the jailbroken phone (diagnostics + recovery) -----------------

def ssh_target() -> tuple[str, str, str]:
    host = CONFIG.get("TH_SSH_HOST") or CONFIG.get("TH_VNC_HOST")
    if not host or host in ("localhost", "127.0.0.1"):
        host = "127.0.0.1"
    return CONFIG.get("TH_SSH_USER") or "mobile", host, CONFIG.get("TH_SSH_PORT") or "22"


def ssh_run(cmd: str, *, check: bool = True, timeout: int = 60, quiet: bool = True):
    user, host, port = ssh_target()
    return run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
                "-p", port, f"{user}@{host}", cmd], check=check, timeout=timeout, quiet=quiet)


def ssh_ok() -> bool:
    return ssh_run("echo ok", check=False, timeout=20).returncode == 0


TROLLVNC_LABEL = "com.82flex.trollvnc"


def vnc_restart() -> str:
    """Restart TrollVNC on the phone. Tries launchctl (needs sudo for the system daemon), then killall
    (launchd's KeepAlive respawns it)."""
    attempts = [
        f"sudo -n launchctl kickstart -k system/{TROLLVNC_LABEL}",
        f"launchctl kickstart -k system/{TROLLVNC_LABEL}",
        "sudo -n killall -9 trollvncserver",
        "killall -9 trollvncserver",
    ]
    for cmd in attempts:
        cp = ssh_run(cmd, check=False, timeout=30)
        if cp.returncode == 0:
            return f"ok: {cmd}"
    raise HarnessError("could not restart TrollVNC over SSH; last error: " + (cp.stderr or cp.stdout)[-300:])


def diag() -> str:
    """One-shot health report from the phone: uptime, TrollVNC process/port, top CPU users, free disk."""
    script = (
        "echo '== uptime'; uptime; "
        "echo '== trollvnc'; ps aux | grep -i [t]rollvnc | head -5; "
        "echo '== listeners'; (netstat -an 2>/dev/null | grep -E 'LISTEN' | grep -E '5901|22' ) || true; "
        "echo '== top'; ps aux -r 2>/dev/null | head -8 || ps aux | sort -rk3 | head -8; "
        "echo '== disk'; df -h /var 2>/dev/null | tail -1; "
        "echo '== tools'; for t in uiopen killall launchctl sudo; do printf '%s: ' $t; command -v $t || echo missing; done"
    )
    return ssh_run(script, check=False, timeout=60).stdout


def ssh_launch(bundle_id: str) -> None:
    """Launch an app by bundle id with Procursus' uiopen (rootless: /var/jb/usr/bin/uiopen)."""
    cp = ssh_run(f"uiopen --bundleid {bundle_id} 2>&1 || /var/jb/usr/bin/uiopen --bundleid {bundle_id}", check=False, timeout=30)
    if cp.returncode != 0:
        raise HarnessError("uiopen failed: " + (cp.stdout + cp.stderr)[-300:])


def ssh_kill(process_name: str) -> None:
    ssh_run(f"killall {process_name}", check=False, timeout=20)


def ssh_installed_apps() -> str:
    cp = ssh_run("uiopen --list 2>/dev/null || ls /var/containers/Bundle/Application/*/ 2>/dev/null", check=False, timeout=30)
    return cp.stdout


def ssh_app_container(bundle_id: str) -> str:
    """Data container of an app on the jailbroken phone (grep the container metadata plists)."""
    cp = ssh_run(f"grep -l '{bundle_id}' /var/mobile/Containers/Data/Application/*/.com.apple.mobile_container_manager.metadata.plist 2>/dev/null | head -1",
                 check=False, timeout=30)
    path = cp.stdout.strip()
    if not path:
        raise HarnessError(f"no data container for {bundle_id} on the phone (is it installed?)")
    return path.rsplit("/", 1)[0]


def ssh_preseed_flags(bundle_id: str, flags: dict[str, bool]) -> str:
    """Write Trio's PersistedProperty plists (Documents/<key>.plist, root Bool) into the app container over SSH."""
    import plistlib, tempfile, subprocess
    container = ssh_app_container(bundle_id)
    user, host, port = ssh_target()
    ssh_run(f"mkdir -p '{container}/Documents'", timeout=20)
    for key, value in flags.items():
        with tempfile.NamedTemporaryFile(suffix=".plist", delete=False) as tf:
            tf.write(plistlib.dumps(value, fmt=plistlib.FMT_BINARY)); local = tf.name
        run(["scp", "-o", "BatchMode=yes", "-q", "-P", port, local, f"{user}@{host}:{container}/Documents/{key}.plist"], timeout=60, quiet=True)
    return container
