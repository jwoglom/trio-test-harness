from __future__ import annotations

import datetime as _dt
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable


class HarnessError(RuntimeError):
    pass


def log(msg: str) -> None:
    ts = _dt.datetime.now().strftime("%H:%M:%S")
    print(f"[th {ts}] {msg}", file=sys.stderr, flush=True)


def run(cmd: list[str] | str, *, check: bool = True, capture: bool = True, env: dict | None = None,
        cwd: str | Path | None = None, timeout: int | None = None, quiet: bool = False,
        input: str | None = None) -> subprocess.CompletedProcess:
    if isinstance(cmd, str):
        shown = cmd
        cmd_list: list[str] | str = cmd
        shell = True
    else:
        shown = shlex.join(cmd)
        cmd_list = cmd
        shell = False
    if not quiet:
        log(f"$ {shown}")
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    try:
        cp = subprocess.run(cmd_list, shell=shell, check=False, text=True, errors="replace", env=full_env, cwd=cwd,
                            timeout=timeout, input=input,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    except subprocess.TimeoutExpired as e:
        raise HarnessError(f"timed out after {timeout}s: {shown}") from e
    if check and cp.returncode != 0:
        err = (cp.stderr or "") + (cp.stdout or "") if capture else ""
        raise HarnessError(f"command failed ({cp.returncode}): {shown}\n{err[-4000:]}")
    return cp


def which(name: str) -> str | None:
    return shutil.which(name)


def now_slug() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, default=str))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def retry(what: str, fn, *, tries: int = 3, delay: float = 10.0, retry_if=None):
    """`fn()`, again up to `tries` times in all when it raises and `retry_if(error)` (default: always) allows it: one
    failed call must not end a run. The last failure is raised."""
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if attempt == tries or (retry_if and not retry_if(e)):
                raise
            log(f"{what} failed ({type(e).__name__}: {str(e)[:300]}); trying again ({attempt + 1}/{tries}) in {delay:g}s")
            time.sleep(delay)


def wait_for(pred, timeout: float, interval: float = 0.5, what: str = "condition") -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(interval)
    raise HarnessError(f"timed out waiting for {what} ({timeout}s)")


def lan_ip() -> str:
    """Best-effort LAN IPv4 of this Mac (the address an iPhone on the same network can reach)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def human_list(items: Iterable[str]) -> str:
    return ", ".join(items)


def tmp_path(name: str) -> Path:
    """Scratch file under runs/.tmp (never /tmp, which may be sandboxed)."""
    from .config import REPO_ROOT
    d = REPO_ROOT / "runs" / ".tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d / name
