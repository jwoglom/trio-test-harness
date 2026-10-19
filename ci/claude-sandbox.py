"""Write the Claude Code settings the CI agent runs under (sandbox + permissions), with this job's paths.

    python ci/claude-sandbox.py OUT.json [TARGET] [APP]   # TARGET: sim (default) or rooted-vnc (`th webui`)
                                                          # APP (trio | loop): the app the agent may change the data of

The agent may only run the harness CLI (plus sleep) through Bash; `--permission-mode dontAsk` and
`--permission-prompts none` deny everything else, and the file tools cannot read outside the working
directories. On top of that, macOS's sandbox (Seatbelt) confines those commands: no network at all,
writes only into the run folders and the temp folders (and, with APP on the simulator, that installed app's own data and app group
containers, for `th db`: harness/appdb.py), and the harness daemons' Unix sockets (the simulator's, and with
it the paired watch's). The daemon is started
outside the sandbox before the agent (it holds the simulator and WebDriverAgent connections, which a
sandboxed process cannot reach), so the agent can do exactly what the daemon's driver exposes.
See https://code.claude.com/docs/en/sandboxing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HARNESS))
from harness.config import daemon_socket  # noqa: E402  (the sockets' path is the harness's to choose)


def temp_dirs() -> list[str]:
    """The temp folders the harness may always use (its daemon sockets live in /tmp): the real /tmp, the user's
    $TMPDIR and, on a runner, $RUNNER_TEMP, each resolved, as Seatbelt matches resolved paths."""
    import os
    import tempfile
    dirs = ["/tmp", tempfile.gettempdir(), os.environ.get("TMPDIR", ""), os.environ.get("RUNNER_TEMP", "")]
    out: list[str] = []
    for d in dirs:
        if d and os.path.isdir(d) and (r := os.path.realpath(d)) not in out:
            out.append(r)
    return out


def settings(target: str = "sim", app: str | None = None) -> dict:
    """`app`: let the harness write that app's simulator containers (its database), looked up now, so the app must
    already be installed. Leave it out for passes that only read the run (highlights)."""
    network = {
        # the apps' documentation only (`th docs`: Trio's, LoopDocs), so the agent can look up where settings live
        "allowedDomains": ["triodocs.org", "loopkit.github.io"],
        "strictAllowlist": True,
        # on the simulator also the paired watch's daemon (`--devices phone+watch`); its socket exists only while it runs
        "allowUnixSockets": [str(daemon_socket(t)) for t in ((target, "watch") if target == "sim" else (target,))],
    }
    app_dirs: list[str] = []
    if target == "sim":
        # read-only simctl queries (`th sim list`); managing the simulator needs writes it does not get
        network["allowMachLookup"] = ["com.apple.coresimulator.*", "com.apple.CoreSimulator.*"]
        if app:
            sys.path.insert(0, str(HARNESS))
            from harness import appdb
            app_dirs = appdb.sandbox_paths(app)
    return {
        # Read, Glob and Grep refuse paths outside the working directories in every mode (Claude Code >= 2.1.257);
        # without it, `-p` runs read outside them unasked.
        "permissions": {"blockReadsOutsideWorkingDirectories": True},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,          # a missing sandbox is an error, not an unsandboxed run
            "allowUnsandboxedCommands": False,  # no dangerouslyDisableSandbox retries
            "autoAllowBashIfSandboxed": False,  # --allowedTools stays the gate for what may run
            "excludedCommands": [],
            "filesystem": {
                "allowWrite": [str(HARNESS / "runs"), str(HARNESS / "build"), *temp_dirs(), *app_dirs],
                # blockReadsOutsideWorkingDirectories also takes reads of /Users away from sandboxed commands, and
                # allowWrite does not give them back; the file tools still cannot read these
                "allowRead": [*temp_dirs(), *app_dirs],
            },
            "network": network,
        },
    }


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(json.dumps(settings(sys.argv[2] if len(sys.argv) > 2 else "sim",
                                                     sys.argv[3] if len(sys.argv) > 3 else None), indent=1) + "\n")
