"""Who is driving a target: the device lock, the one canonical answer (`th busy`).

    th busy                      # every target: idle, or who holds it
    th --target rooted-vnc busy  # one target; exit 1 while busy

Whatever drives a target holds runs/.tmp/device-<target>.lock with an OS file lock (flock) and writes what it is
into the file: trio-test-e2e for its whole run (harness/pipeline.py), and a web UI job for its agent and review
stages (harness/webui.py). The OS drops the lock the moment its process exits, even on a crash, so it is never stale;
the text left in an unlocked file means nothing.

Nothing else counts. In particular, do not infer a run from process lists: an idle interactive `claude` session
started as `claude ... ./trio-test-e2e --pr ...` keeps that text in its command line for days and drives nothing.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .config import REPO_ROOT
from .util import log

LOCK_DIR = REPO_ROOT / "runs" / ".tmp"
TARGETS = ("sim", "rooted-vnc", "iphone-usb")


def lock_path(target: str) -> Path:
    # the watch simulator is paired with the iPhone simulator and driven in the same run
    return LOCK_DIR / f"device-{'sim' if target == 'watch' else target}.lock"


def holder(target: str) -> dict | None:
    """Who holds `target`'s lock ({"what", "pid", "since", ...}), or None when nobody does."""
    p = lock_path(target)
    if not p.exists():
        return None
    try:
        with open(p, "r+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fh.seek(0)
                try:
                    return json.loads(fh.read() or "{}") or {"what": "unknown holder"}
                except ValueError:
                    return {"what": "unknown holder"}
            fh.seek(0)
            fh.truncate()                          # nobody holds it: clear what a dead holder left behind
            fh.flush()
            fcntl.flock(fh, fcntl.LOCK_UN)
            return None
    except OSError:
        return None


def describe(h: dict) -> str:
    since = h.get("since", "")
    return f"{h.get('what', '?')} (pid {h.get('pid', '?')}, since {since.replace('T', ' ')[:16]})"


@contextmanager
def hold(target: str, what: str, *, wait: bool = True, on_wait=None, cancelled=lambda: False, **extra):
    """Hold `target`'s lock for the duration. While another process holds it: wait (calling on_wait(holder) once)
    until it is free or cancelled() is true, or raise when wait=False. Yields False if cancelled while waiting."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    p = lock_path(target)
    fh = open(p, "a+")
    announced = False
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait:
                    from .util import HarnessError
                    raise HarnessError(f"{target} is busy: {describe(holder(target) or {})}") from None
                if cancelled():
                    yield False
                    return
                if not announced:
                    h = holder(target) or {}
                    (on_wait or (lambda h: log(f"waiting for {target}: {describe(h)}")))(h)
                    announced = True
                time.sleep(5)
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"what": what, "pid": os.getpid(), "target": target,
                             "since": dt.datetime.now().isoformat(timespec="seconds"), **extra}) + "\n")
        fh.flush()
        try:
            yield True
        finally:
            fh.seek(0)
            fh.truncate()                          # released: leave no text that looks like a holder
            fh.flush()
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()
