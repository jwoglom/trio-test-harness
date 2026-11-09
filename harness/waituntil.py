"""`wait --until "<harness subcommand>" --matches REGEX`: run a subcommand again and again, in this process and with no
shell, until a line of its output matches. How a job's agent polls: its sandbox allows no `until ...; do` loop and
Claude Code blocks a long `sleep`, so the loop lives here, under the same cap as a plain `wait`."""
from __future__ import annotations

import io
import re
import shlex
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from typing import Callable

from .util import HarnessError

PROGRESS_EVERY = 30.0


@dataclass
class Outcome:
    line: str | None        # the first matching line of the last run, None on a timeout
    attempts: int
    elapsed: float
    last_output: str = ""
    last_error: str = ""


def first_match(rx: re.Pattern, text: str) -> str | None:
    return next((ln for ln in text.splitlines() if rx.search(ln)), None)


def wait_until(run_once: Callable[[], tuple[int, str, str]], pattern: str, *, timeout: float, every: float,
               ignore_case: bool = False, sleep: Callable[[float], None] = time.sleep,
               clock: Callable[[], float] = time.monotonic, progress: Callable[[str], None] | None = None) -> Outcome:
    """Calls `run_once` (returns exit code, stdout, stderr) every `every` seconds until a stdout line matches `pattern`
    or `timeout` seconds have passed. It runs at least once, and never starts a run after the deadline."""
    try:
        rx = re.compile(pattern, re.I if ignore_case else 0)
    except re.error as e:
        raise HarnessError(f"--matches {pattern!r} is not a regular expression: {e}") from None
    start = clock()
    attempts, last_out, last_err, next_report = 0, "", "", start + PROGRESS_EVERY
    while True:
        attempts += 1
        rc, out, err = run_once()
        last_out, last_err = out, (err.strip() or (f"exit code {rc}" if rc else ""))
        line = first_match(rx, out)
        if line is not None:
            return Outcome(line, attempts, clock() - start, out, last_err)
        now = clock()
        left = timeout - (now - start)
        if left <= 0:
            return Outcome(None, attempts, now - start, last_out, last_err)
        if progress and now >= next_report:
            progress(f"still waiting ({now - start:.0f} of {timeout:g} s, {attempts} runs)")
            next_report = now + PROGRESS_EVERY
        sleep(min(every, left))


def capture(main: Callable[[list[str]], int], argv: list[str]) -> tuple[int, str, str]:
    """`main(argv)` in this process with stdout and stderr captured; an argparse exit is a non-zero code."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = main(argv)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 2
    return rc or 0, out.getvalue(), err.getvalue()


def split_command(text: str) -> list[str]:
    try:
        argv = shlex.split(text)
    except ValueError as e:
        raise HarnessError(f"--until {text!r}: {e}") from None
    if not argv:
        raise HarnessError("--until needs a harness subcommand, e.g. --until \"logs trio --since 2m\"")
    if argv[0] in ("./trio-test-harness", "trio-test-harness", "./bin/th", "bin/th", "th"):
        argv = argv[1:]
    return argv
