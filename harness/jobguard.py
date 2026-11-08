"""No job may start another job.

A harness job (the CI workflow, a web UI job) ends with a sandboxed `claude -p` agent that may run the harness CLI.
The entry points that start a job are refused under that agent, so a job can never start jobs without end:
`trio-test-e2e` (harness.pipeline), `trio-test-harness webui` and `trio-test-ci` (which checks the same variable
itself; it is standalone). The sandbox (ci/claude-sandbox.py: no network, the harness CLI only) blocks these too;
this check does not depend on it.

The job sets TH_JOB_AGENT=1 for its agent and its highlights review; Claude Code passes it to every command they run.
"""
from __future__ import annotations

import os
import sys

ENV = "TH_JOB_AGENT"


def inside_job_agent() -> bool:
    return os.environ.get(ENV) == "1"


def refuse_inside_job_agent(what: str, reason: str = "a job must not start another job") -> None:
    """Exits with status 2 when running under a job's agent."""
    if inside_job_agent():
        print(f"{what}: refused: this is a harness job's agent ({ENV}=1), and {reason}", file=sys.stderr)
        raise SystemExit(2)
