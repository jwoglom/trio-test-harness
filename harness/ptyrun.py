"""Run an interactive `claude` in a pseudo-terminal with nobody at the keyboard, and report its claude.ai/code URL.

    python -m harness.ptyrun --log LOG --url-file URL [--cwd DIR] [--accept-trust] [--exit-after-url] -- claude ...

`claude --remote-control` and `claude --cloud "task"` are interactive only: they need a terminal, and print the
session's https://claude.ai/code/session_... link into it. This keeps the terminal output flowing into LOG (a TUI
blocks once nobody reads its terminal), answers the cursor-position query, and writes the first session link to
URL-FILE. With --accept-trust it answers "Yes, I trust this folder" (a throwaway checkout the harness just made).
With --exit-after-url it stops the child once the link is out (`--cloud` has nothing more to do locally).
Started detached (setsid) by the web UI, so a Remote Control session outlives a web UI restart; SIGTERM ends it.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time

URL = re.compile(r"https://claude\.ai/code/(session_[A-Za-z0-9]+)")
# a redraw can glue the link to the next word once whitespace is dropped; ids are session_ + 24 characters
URL_FLAT = re.compile(r"https://claude\.ai/code/(session_[A-Za-z0-9]{24})")
CSI = re.compile(rb"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[=>78]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--url-file", required=True)
    ap.add_argument("--cwd")
    ap.add_argument("--accept-trust", action="store_true")
    ap.add_argument("--exit-after-url", action="store_true")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("no command")

    pid, fd = pty.fork()
    if pid == 0:
        if a.cwd:
            os.chdir(a.cwd)
        os.environ["TERM"] = "xterm-256color"
        try:
            os.execvp(cmd[0], cmd)
        finally:
            os._exit(127)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))

    def stop(*_):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)

    tail, url, trusted, url_at, status = b"", None, False, 0.0, None
    with open(a.log, "ab", buffering=0) as log:
        while True:
            try:
                r, _, _ = select.select([fd], [], [], 1)
            except InterruptedError:
                continue
            if r:
                try:
                    data = os.read(fd, 65536)
                except OSError:          # EIO: the child closed the terminal
                    break
                if not data:
                    break
                log.write(data)
                if b"\x1b[6n" in data:
                    os.write(fd, b"\x1b[1;1R")
                tail = (tail + data)[-32768:]
                text = CSI.sub(b"", tail).decode("utf8", "replace")
                flat = "".join(text.split())
                if a.accept_trust and not trusted and "Yes,Itrustthisfolder" in flat:
                    trusted = True
                    time.sleep(0.5)
                    os.write(fd, b"\x1b[B")
                    time.sleep(0.3)
                    os.write(fd, b"\r")
                if not url and (m := URL.search(text) or URL_FLAT.search(flat)):
                    url = f"https://claude.ai/code/{m.group(1)}"
                    tmp = a.url_file + ".tmp"
                    with open(tmp, "w") as fh:
                        fh.write(url + "\n")
                    os.replace(tmp, a.url_file)
                    url_at = time.time()
            if a.exit_after_url and url and time.time() - url_at > 3:
                stop()
                a.exit_after_url = False
            done, st = os.waitpid(pid, os.WNOHANG)
            if done:
                status = st
                break
    if status is None:
        try:
            status = os.waitpid(pid, 0)[1]
        except ChildProcessError:
            status = 0
    return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    sys.exit(main())
