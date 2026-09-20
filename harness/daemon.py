"""Daemon mode: keep one driver (VNC connection / WDA session) alive in a
background process and let every `th` invocation reuse it.

    th daemon start --target device     # forks; socket at runs/.tmp/daemon-device.sock
    th daemon status|stop

When the socket answers a ping, `cli.get_driver()` transparently returns a
RemoteDriver proxy, so all commands (shot, tap, tap-text, flows, ...) use the
persistent connection. Protocol: one JSON object per line over a Unix socket:
  {"method": "tap", "args": [x, y], "kwargs": {}}  ->  {"ok": true, "result": ...}
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

from .config import REPO_ROOT
from .driver import Driver, UINode
from .util import HarnessError, log

SOCK_DIR = REPO_ROOT / "runs" / ".tmp"
ALLOWED_PRIVATE = {"unlock", "is_dark", "open_app_via_spotlight", "long_press", "power", "paste_text", "wake", "boot", "launch",
                   "terminate", "install", "uninstall", "is_installed", "open_url", "app_data_container",
                   "defaults_write", "ensure_wda", "describe_point"}


def sock_path(target: str) -> Path:
    return SOCK_DIR / f"daemon-{target}.sock"


def pid_path(target: str) -> Path:
    return SOCK_DIR / f"daemon-{target}.pid"


def log_path(target: str) -> Path:
    return SOCK_DIR / f"daemon-{target}.log"


# ---------------- serialization -----------------

def _encode(obj):
    if isinstance(obj, Path):
        return {"__path__": str(obj)}
    if isinstance(obj, UINode):
        return {"__uinode__": obj.__dict__}
    if isinstance(obj, tuple):
        return {"__tuple__": [_encode(x) for x in obj]}
    if isinstance(obj, list):
        return [_encode(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    return obj


def _decode(obj):
    if isinstance(obj, dict):
        if "__path__" in obj:
            return Path(obj["__path__"])
        if "__uinode__" in obj:
            return UINode(**obj["__uinode__"])
        if "__tuple__" in obj:
            return tuple(_decode(x) for x in obj["__tuple__"])
        return {k: _decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode(x) for x in obj]
    return obj


# ---------------- server -----------------

def _make_driver(target: str) -> Driver:
    if target == "sim":
        from .sim import SimulatorDriver
        return SimulatorDriver()
    from .vnc import VNCDriver
    return VNCDriver()


def serve(target: str) -> None:
    sp, pp = sock_path(target), pid_path(target)
    SOCK_DIR.mkdir(parents=True, exist_ok=True)
    if sp.exists():
        sp.unlink()
    drv = _make_driver(target)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sp))
    srv.listen(4)
    pp.write_text(str(os.getpid()))
    log(f"daemon[{target}] listening on {sp}")

    def _shutdown(*_):
        log(f"daemon[{target}] shutting down")
        try:
            close = getattr(drv, "close", None)
            if close:
                close()
        finally:
            for p in (sp, pp):
                if p.exists():
                    p.unlink()
            sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while True:
        conn, _ = srv.accept()
        with conn, conn.makefile("rwb") as f:
            for line in f:
                try:
                    req = json.loads(line)
                    method, args, kwargs = req["method"], _decode(req.get("args", [])), _decode(req.get("kwargs", {}))
                    if method == "ping":
                        resp = {"ok": True, "result": {"target": target, "pid": os.getpid(), "kind": drv.kind}}
                    elif method == "shutdown":
                        f.write(b'{"ok": true, "result": "bye"}\n'); f.flush()
                        _shutdown()
                    else:
                        if method.startswith("_") and method not in ALLOWED_PRIVATE:
                            raise HarnessError(f"method {method} not allowed")
                        fn = getattr(drv, method)
                        try:
                            result = fn(*args, **kwargs)
                        except (OSError, HarnessError) as e:
                            # connection probably died (phone slept, VNC restarted): reset and retry once
                            log(f"daemon[{target}] {method} failed ({e}); reconnecting")
                            close = getattr(drv, "close", None)
                            if close:
                                close()
                            result = fn(*args, **kwargs)
                        resp = {"ok": True, "result": _encode(result)}
                except Exception as e:  # noqa: BLE001
                    resp = {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-2000:]}
                f.write((json.dumps(resp) + "\n").encode()); f.flush()


# ---------------- client -----------------

def is_running(target: str, timeout: float = 2.0) -> bool:
    sp = sock_path(target)
    if not sp.exists():
        return False
    try:
        RemoteDriver(target, timeout=timeout).ping()
        return True
    except Exception:  # noqa: BLE001
        return False


class RemoteDriver(Driver):
    """Proxy that forwards every driver method to the daemon."""

    def __init__(self, target: str, timeout: float = 600.0):
        self.target = target
        self.kind = "sim" if target == "sim" else "device"
        self._timeout = timeout

    def _call(self, method: str, *args, **kwargs):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self._timeout)
        s.connect(str(sock_path(self.target)))
        with s, s.makefile("rwb") as f:
            f.write((json.dumps({"method": method, "args": _encode(list(args)), "kwargs": _encode(kwargs)}) + "\n").encode())
            f.flush()
            line = f.readline()
        if not line:
            raise HarnessError("daemon closed the connection")
        resp = json.loads(line)
        if not resp.get("ok"):
            err = resp.get("error", "daemon error")
            if err.startswith("LookupError"):
                raise LookupError(err.split(": ", 1)[-1])
            raise HarnessError(err)
        return _decode(resp.get("result"))

    def ping(self) -> dict:
        return self._call("ping")

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: self._call(name, *a, **k)

    # Driver's helper methods (find/tap_text/wait_for_text/scroll) are implemented in terms of the
    # primitives; forwarding them whole to the daemon keeps a single round-trip and lets the
    # simulator's fast predicate search run server-side.
    def find(self, *a, **k):
        return self._call("find", *a, **k)

    def tap_text(self, *a, **k):
        return self._call("tap_text", *a, **k)

    def wait_for_text(self, *a, **k):
        return self._call("wait_for_text", *a, **k)

    def scroll(self, *a, **k):
        return self._call("scroll", *a, **k)

    def ui_tree(self, *a, **k):
        return self._call("ui_tree", *a, **k)

    def screenshot(self, path: Path) -> Path:
        return self._call("screenshot", Path(path).resolve())

    def screen_size(self):
        return tuple(self._call("screen_size"))

    def tap(self, x, y, *, duration: float = 0.0):
        return self._call("tap", x, y, duration=duration)

    def swipe(self, x1, y1, x2, y2, *, duration: float = 0.3):
        return self._call("swipe", x1, y1, x2, y2, duration=duration)

    def type_text(self, text):
        return self._call("type_text", text)

    def key(self, name):
        return self._call("key", name)

    def home(self):
        return self._call("home")

    def wake(self):
        return self._call("wake")

    def boot(self):
        return self._call("boot")

    def launch(self, bundle_id, *, terminate_first: bool = True):
        return self._call("launch", bundle_id, terminate_first=terminate_first)

    def terminate(self, bundle_id):
        return self._call("terminate", bundle_id)

    def install(self, app_path):
        return self._call("install", Path(app_path).resolve())


# ---------------- process management -----------------

def start(target: str, foreground: bool = False) -> None:
    if is_running(target):
        log(f"daemon[{target}] already running")
        return
    if foreground:
        serve(target)
        return
    SOCK_DIR.mkdir(parents=True, exist_ok=True)
    with open(log_path(target), "a") as fh:
        p = subprocess.Popen([sys.executable, "-m", "harness.daemon", "--target", target], stdout=fh, stderr=fh,
                             cwd=str(REPO_ROOT), start_new_session=True)
    deadline = time.time() + 60
    while time.time() < deadline:
        if is_running(target):
            log(f"daemon[{target}] started (pid {p.pid}, log {log_path(target)})")
            return
        if p.poll() is not None:
            break
        time.sleep(0.5)
    tail = log_path(target).read_text()[-1200:] if log_path(target).exists() else ""
    raise HarnessError(f"daemon[{target}] failed to start\n{tail}")


def stop(target: str) -> None:
    if is_running(target):
        try:
            RemoteDriver(target, timeout=10)._call("shutdown")
        except Exception:  # noqa: BLE001
            pass
    pp = pid_path(target)
    if pp.exists():
        try:
            os.kill(int(pp.read_text()), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass
        pp.unlink(missing_ok=True)
    sock_path(target).unlink(missing_ok=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="device")
    serve(ap.parse_args().target)
