"""Write the Claude Code settings the CI agent runs under (sandbox + permissions), with this job's paths.

    python ci/claude-sandbox.py OUT.json [TARGET]     # TARGET: sim (default) or rooted-vnc (`th webui`)

The agent may only run the harness CLI (plus sleep) through Bash; `--permission-mode dontAsk` and
`--permission-prompts none` deny everything else, and the file tools cannot read outside the working
directories. On top of that, macOS's sandbox (Seatbelt) confines those commands: no network at all,
writes only into the run folders, and a single Unix socket, the harness daemon's. The daemon is started
outside the sandbox before the agent (it holds the simulator and WebDriverAgent connections, which a
sandboxed process cannot reach), so the agent can do exactly what the daemon's driver exposes.
See https://code.claude.com/docs/en/sandboxing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parent.parent


def settings(target: str = "sim") -> dict:
    network = {
        # the Trio documentation only (`th docs`), so the agent can look up where settings live
        "allowedDomains": ["triodocs.org"],
        "strictAllowlist": True,
        "allowUnixSockets": [str(HARNESS / "runs" / ".tmp" / f"daemon-{target}.sock")],
    }
    if target == "sim":
        # read-only simctl queries (`th sim list`); managing the simulator needs writes it does not get
        network["allowMachLookup"] = ["com.apple.coresimulator.*", "com.apple.CoreSimulator.*"]
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
                "allowWrite": [str(HARNESS / "runs"), str(HARNESS / "build")],
            },
            "network": network,
        },
    }


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(json.dumps(settings(sys.argv[2] if len(sys.argv) > 2 else "sim"), indent=1) + "\n")
