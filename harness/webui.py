"""`trio-test-harness webui`: run the e2e job from a browser, on this machine, and post the PR comment on demand.

    ./trio-test-harness webui                  # prints http://127.0.0.1:8787/?token=...
    ./trio-test-harness webui --port 9000 --open
    ./trio-test-harness webui --host 0.0.0.0        # other machines too (or TH_WEBUI_HOST in harness.env)

The form has the options of .github/workflows/e2e.yml (and ./trio-test-ci), plus the target: rooted-vnc (the
jailbroken iPhone, default) or simulator. A job runs the workflow's stages here, with this Mac's harness.env, gh
login and claude login:

  inputs (ci/inputs.py) -> trio-test-e2e -> agent (claude -p, sandboxed as in CI) -> highlights review -> report.html

Nothing goes to GitHub until "Post PR comment" is pressed (or "post when done" was ticked). Jobs run one at a time
(one phone, one simulator) and first wait for any other trio-test-e2e on this machine. Each job's state is in
runs/.webui/<job>/ (job.json, log.txt, claude-settings.json); the run itself is a normal runs/<run> directory.

A finished job can be continued (harness/handoff.py): the agent's own conversation resumed on this Mac with full access
through Remote Control (optionally in a git worktree copy of the Trio checkout), or a Claude Code cloud session.

Access needs the token in the printed URL (kept in runs/.tmp/webui-token, stored as a SameSite=Strict cookie):
the server can build, drive the phone and comment as you, so other pages and other users must not reach it.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import queue
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import CONFIG, REPO_ROOT
from .util import HarnessError, lan_ip, log, read_json, write_json

JOBS_DIR = CONFIG.runs_dir / ".webui"
TOKEN_FILE = REPO_ROOT / "runs" / ".tmp" / "webui-token"
COOKIE = "th_webui"
TARGETS = {"rooted-vnc": "rooted-vnc", "simulator": "sim"}      # form value -> harness target
ACTIVE = {"queued", "waiting", "running"}
PAGE = Path(__file__).with_name("webui.html")
SCREENS_DIR = JOBS_DIR / "screens"
SHOT_NAME = re.compile(r"^[\w.-]+\.png$")
MODEL = re.compile(r"^[\w.:\[\]-]{1,80}$")


def _ci(name: str):
    """A module from ci/ (not a package; claude-sandbox has a dash in its name)."""
    spec = importlib.util.spec_from_file_location("ci_" + name.replace("-", "_"), REPO_ROOT / "ci" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ---------------- jobs -----------------

class Job:
    def __init__(self, path: Path, data: dict):
        self.dir, self.data = path, data
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self._continuations = None
        self._steps: list[str] = []      # trio-test-e2e steps seen in log.txt ("== stage: build"), in order
        self._steps_off = 0

    @property
    def continuations(self):
        from .handoff import Continuations
        if self._continuations is None:
            self._continuations = Continuations(self.dir)
        return self._continuations

    @property
    def id(self) -> str:
        return self.dir.name

    @property
    def log_path(self) -> Path:
        return self.dir / "log.txt"

    def save(self) -> None:
        with self.lock:
            tmp = self.dir / "job.json.tmp"
            write_json(tmp, self.data)
            tmp.replace(self.dir / "job.json")

    def update(self, **kw) -> None:
        with self.lock:
            self.data.update(kw)
            self.save()

    def note(self, msg: str) -> None:
        with open(self.log_path, "a") as fh:
            fh.write(f"\n[webui {dt.datetime.now():%H:%M:%S}] {msg}\n")

    def stage(self, name: str, status: str, note: str = "") -> None:
        with self.lock:
            st = self.data.setdefault("stages", {}).setdefault(name, {})
            st["status"] = status
            st["started" if status == "running" else "ended"] = _now()
            if note:
                st["note"] = note
            self.save()

    def sh(self, cmd: list[str], env: dict, timeout: float | None = None) -> tuple[int, str]:
        """Run cmd with its output appended to log.txt; returns (exit code or -1 on timeout/cancel, its output)."""
        if self.cancelled:
            return -1, ""
        self.note("$ " + " ".join(cmd if cmd[0] != "bash" else cmd[:2] + ["…"]))
        start = self.log_path.stat().st_size
        with open(self.log_path, "a") as fh:
            p = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
            self.proc = p
            try:
                rc = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.note(f"time limit ({timeout / 60:.0f} min) reached; stopping")
                _kill(p)
                rc = -1
            finally:
                self.proc = None
        with open(self.log_path, "rb") as fh:
            fh.seek(start)
            out = fh.read().decode(errors="replace")
        return (-1 if self.cancelled else rc), out

    def e2e_steps(self) -> list[str]:
        """The trio-test-e2e steps reached so far (checkout, build, install, launch, smoke, report), read from the new
        part of log.txt only: a build log is several MB."""
        with self.lock:
            if self.data.get("e2e_steps") and (self.data.get("stages", {}).get("e2e") or {}).get("status") != "running":
                return self.data["e2e_steps"]
            try:
                with open(self.log_path, "rb") as fh:
                    fh.seek(self._steps_off)
                    chunk = fh.read()
            except OSError:
                return self._steps
            end = chunk.rfind(b"\n") + 1           # a partial last line is read again next time
            self._steps_off += end
            for m in re.finditer(rb"== stage: ([a-z]+)", chunk[:end]):
                name = m.group(1).decode()
                if name not in self._steps:
                    self._steps.append(name)
            return self._steps

    def cancel(self) -> None:
        self.cancelled = True
        if self.proc:
            _kill(self.proc)

    def run_dir(self) -> Path | None:
        rd = self.data.get("run_dir")
        return Path(rd) if rd and Path(rd).is_dir() else None

    def public(self, full: bool = False) -> dict:
        with self.lock:
            d = dict(self.data, id=self.id)
        rd = self.run_dir()
        d["has_report"] = bool(rd and (rd / "report.html").exists())
        d["e2e_steps"] = self.e2e_steps()
        if rd:
            meta = read_json(rd / "run.json", {}) or {}
            d["run_status"], d["run_summary"] = meta.get("status"), meta.get("summary")
            d["run_name"] = rd.name
        if full and rd:
            s = rd / "agent-summary.md"
            d["agent_summary"] = s.read_text() if s.exists() else ""
            hl = read_json(rd / "highlights.json", None)
            d["verdict"] = hl.get("verdict") if isinstance(hl, dict) else None
            from . import handoff as H
            d["continuations"] = self.continuations.list()
            d["agent_session"] = H.agent_session(rd, self.data.get("agent_session"))
            if d["agent_session"] and d["agent_session"] != self.data.get("agent_session"):
                self.update(agent_session=d["agent_session"])      # found by its prompt (older job): look once
            d["pr_session"] = H.pr_session(rd)
        return d


def _kill(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _base_env() -> dict:
    """This environment minus Claude Code's session variables (the webui may be started from inside Claude Code;
    the agent's `claude -p` must be a fresh top-level session, and trio-test-e2e must not think it is nested)."""
    env = {k: v for k, v in os.environ.items()
           if k != "CLAUDECODE" and not (k.startswith("CLAUDE_CODE_") and k != "CLAUDE_CODE_OAUTH_TOKEN")}
    env.pop("TH_RUN", None)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _job_env(inp: dict) -> dict:
    env = _base_env()
    env["TH_BUILD_LOG_STDOUT"] = "1"      # the full xcodebuild output streams into the job log
    if inp.get("xcode"):
        env["TH_XCODE_APP"] = inp["xcode"]
    if inp["target"] == "simulator":
        if inp.get("sim_device"):
            env["TH_SIM_DEVICE"] = inp["sim_device"]
        if inp.get("sim_runtime"):
            env["TH_SIM_RUNTIME"] = inp["sim_runtime"]
    return env


def _server_alive(pid) -> bool:
    """Is `pid` another running `trio-test-harness webui`?"""
    if not isinstance(pid, int) or pid == os.getpid():
        return False
    cp = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return "webui" in cp.stdout




class Manager:
    def __init__(self):
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self.queue: queue.Queue[str] = queue.Queue()
        for d in sorted(JOBS_DIR.iterdir()):
            data = read_json(d / "job.json", None) if d.is_dir() else None
            if not isinstance(data, dict):
                continue
            job = Job(d, data)
            if data.get("status") in ACTIVE and _server_alive(data.get("server_pid")):
                continue                               # another web UI (a second instance) owns it: leave it alone
            if data.get("status") in ACTIVE:           # the server stopped while it was queued or running
                for st in (data.get("stages") or {}).values():
                    if st.get("status") == "running":
                        st["status"] = "interrupted"
                job.update(status="interrupted")
            if (data.get("comment") or {}).get("status") == "posting":
                job.update(comment={**data["comment"], "status": "unknown",
                                    "error": "the server stopped while posting; check the PR"})
            self.jobs[job.id] = job
        threading.Thread(target=self._worker, daemon=True, name="webui-worker").start()

    # --- submission ---

    def submit(self, form: dict) -> Job:
        inp = self.validate(form)
        e2e_args, pr = _ci("inputs").plan(inp["trio"], inp["submodules"], inp["cherry_pick"], inp["submodule_cherry_pick"])
        jid = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        while (JOBS_DIR / jid).exists():
            jid += "x"
        (JOBS_DIR / jid).mkdir(parents=True)
        job = Job(JOBS_DIR / jid, {"created": _now(), "status": "queued", "inputs": inp, "e2e_args": e2e_args,
                                   "pr": pr, "stages": {}, "comment": None, "server_pid": os.getpid()})
        job.save()
        job.log_path.touch()
        self.jobs[jid] = job
        self.queue.put(jid)
        return job

    @staticmethod
    def validate(f: dict) -> dict:
        def text(k, n=300):
            v = str(f.get(k) or "").strip()
            if len(v) > n:
                raise ValueError(f"{k}: too long")
            return v
        target = text("target") or "rooted-vnc"
        if target not in TARGETS:
            raise ValueError(f"target must be one of {', '.join(TARGETS)}")
        conf = text("configuration") or "Debug"
        if conf not in ("Debug", "Release"):
            raise ValueError("configuration must be Debug or Release")
        model = text("claude_model") or "sonnet"
        if not MODEL.match(model):
            raise ValueError("claude_model: letters, digits and . : - [ ] only")
        try:
            minutes = int(f.get("agent_minutes") or 30)
        except (TypeError, ValueError):
            raise ValueError("agent_minutes must be a number") from None
        if not 1 <= minutes <= 180:
            raise ValueError("agent_minutes must be between 1 and 180")
        xcode = text("xcode")
        if xcode and xcode not in _xcodes():
            raise ValueError(f"xcode: {xcode} is not installed")
        return {"target": target, "trio": text("trio"), "submodules": text("submodules", 1000),
                "cherry_pick": text("cherry_pick", 1000), "submodule_cherry_pick": text("submodule_cherry_pick", 1000),
                "configuration": conf, "skip_onboarding": bool(f.get("skip_onboarding", True)),
                "cgm_sim": bool(f.get("cgm_sim", f.get("smoke", True))),
                "pump_sim": bool(f.get("pump_sim", f.get("smoke", True))), "claude_model": model,
                "agent_instructions": text("agent_instructions", 4000), "agent_minutes": minutes,
                "post_comment": bool(f.get("post_comment")), "xcode": xcode, "agent": bool(f.get("agent", True)),
                "sim_device": text("sim_device", 100), "sim_runtime": text("sim_runtime", 100)}

    # --- running ---

    def _worker(self) -> None:
        while True:
            job = self.jobs.get(self.queue.get())
            if not job or job.cancelled:
                continue
            try:
                self._wait_for_others(job)
                if not job.cancelled:
                    job.update(status="running", started=_now())
                    run_job(job)
            except Exception as e:  # noqa: BLE001
                job.note(f"webui error: {type(e).__name__}: {e}")
                job.update(status="failed", error=str(e))
            if job.cancelled:
                job.update(status="cancelled")
            job.update(finished=_now())

    @staticmethod
    def _wait_for_others(job: Job) -> None:
        """Wait while anything holds the job's target (the device lock, harness/busy.py; `th busy` shows it)."""
        from . import busy
        target, announced = TARGETS[job.data["inputs"]["target"]], False
        while not job.cancelled and (h := busy.holder(target)):
            if not announced:
                job.update(status="waiting")
                job.note(f"waiting for {target}: {busy.describe(h)}")
                announced = True
            time.sleep(5)


def _sims(inp: dict) -> list[str]:
    """The fake devices a job sets up (jobs from before the split have one "smoke" flag)."""
    old = inp.get("smoke", True)
    return [n for n, k in (("Glucose Simulator", "cgm_sim"), ("Pump Simulator", "pump_sim")) if inp.get(k, old)]


def run_job(job: Job) -> None:
    """The workflow's steps: e2e -> agent -> highlights -> report.html -> (optional) PR comment."""
    inp = job.data["inputs"]
    target = TARGETS[inp["target"]]
    env = _job_env(inp)
    py = sys.executable
    extra = f"\nFocus from the person who started this run:\n{inp['agent_instructions']}\n" if inp["agent_instructions"] else ""

    # --- trio-test-e2e: checkout, build, install, launch, smoke, report ---
    job.stage("e2e", "running")
    cmd = ["./trio-test-e2e", "--mode", inp["target"], "--yes", "--no-agent", "--comment", "never",
           "--configuration", inp["configuration"], "--onboarding", "preseed" if inp["skip_onboarding"] else "walk",
           *job.data["e2e_args"]]
    # The fake CGM/pump setup steps are left out of report.html and the comment, as in CI.
    sims = _sims(inp)
    cmd += ["--hide-setup-screenshots"] if sims else ["--no-smoke"]
    cmd += ["--no-cgm-sim"] if sims and "Glucose Simulator" not in sims else []
    cmd += ["--no-pump-sim"] if sims and "Pump Simulator" not in sims else []
    rc, out = job.sh(cmd, env)
    # The pipeline prints its run directory at the end ("run: <dir>"); runs/.current may belong to another session.
    m = re.findall(r"^run:\s+(/\S.*)$", out, re.M)
    if m:
        job.update(run_dir=m[-1].strip())
    job.update(e2e_steps=list(job.e2e_steps()))
    job.stage("e2e", "passed" if rc == 0 else "failed")
    rd = job.run_dir()

    # --- agent: Claude Code tests what the PR describes, sandboxed as in CI ---
    if rc == 0 and rd and not inp.get("agent", True):
        job.stage("agent", "skipped", "Trigger agent was off")
    elif rc == 0 and rd:
        job.stage("agent", "running")
        settings = job.dir / "claude-settings.json"
        settings.write_text(json.dumps(_ci("claude-sandbox").settings(target), indent=1) + "\n")
        prompt_file, limits_file = (("agent-prompt.md", "agent-limits.md") if target == "sim"
                                    else ("agent-prompt-rooted-vnc.md", "agent-limits-rooted-vnc.md"))
        smoke_note = f", with the {' and '.join(sims)} configured" if sims else ""
        prompt = (REPO_ROOT / "ci" / prompt_file).read_text().format(run_dir=rd, smoke_note=smoke_note, extra=extra)
        from . import daemon as D
        # The daemon holds the VNC/WDA connection outside the sandbox. Another session may already run one:
        # use it, and leave it running afterwards.
        started = False
        if not D.is_running(target):
            started = job.sh(["./trio-test-harness", "--target", target, "daemon", "start"], env)[0] == 0
        # A known session id, so "Continue conversation" can resume it (harness/handoff.py).
        sid = str(uuid.uuid4())
        job.update(agent_session=sid)
        # The agent drives the device: hold its lock (another run may have taken it since the e2e step ended).
        from . import busy
        lock = busy.hold(target, f"web UI job {job.id}: agent ({job.data.get('pr') or inp['trio'] or 'dev'})",
                         on_wait=lambda h: job.note(f"waiting for {target}: {busy.describe(h)}"),
                         cancelled=lambda: job.cancelled)
        lock.__enter__()
        agent_env = {**env, "TH_RUN": str(rd), "PROMPT": prompt, "MODEL": inp["claude_model"], "SETTINGS": str(settings),
                     "SID": sid,
                     "LIMITS": (REPO_ROOT / "ci" / limits_file).read_text(), "OUT": str(rd / "agent-summary.md"), "PY": py}
        arc = -1
        try:
            arc, _ = job.sh(["bash", "-c",
                             'set -o pipefail; claude -p "$PROMPT" --model "$MODEL" --output-format stream-json --verbose '
                             '--session-id "$SID" '
                             '--permission-mode dontAsk --permission-prompts none --settings "$SETTINGS" '
                             '--append-system-prompt "$LIMITS" '
                             '--allowedTools "Bash(./trio-test-harness:*)" "Bash(./bin/th:*)" "Bash(sleep:*)" Read Glob Grep '
                             '| "$PY" ci/agent-stream.py "$OUT"'], agent_env, timeout=inp["agent_minutes"] * 60)
        finally:
            lock.__exit__(None, None, None)
            if started:
                D.stop(target)
                job.note(f"stopped the {target} daemon")
            _render(job, rd)
        job.stage("agent", "passed" if arc == 0 else "failed")   # continue-on-error, as in CI

    # --- highlights: a separate pass with no memory of the session picks the relevant screenshots ---
    # Without an agent nothing was tested beyond launch/setup, so there is nothing to pick (the comment has no shots).
    if rd and not job.cancelled and not inp.get("agent", True):
        job.stage("highlights", "skipped", "no agent ran")
    elif rd and not job.cancelled:
        job.stage("highlights", "running")
        prompt = (REPO_ROOT / "ci" / "highlights-prompt.md").read_text().format(run_dir=rd, extra=extra)
        if target != "sim":
            prompt = prompt.replace("on the iOS Simulator", "on a jailbroken test iPhone")
        settings = job.dir / "claude-settings-review.json"
        settings.write_text(json.dumps(_ci("claude-sandbox").settings(target), indent=1) + "\n")
        hl_env = {**env, "PROMPT": prompt, "MODEL": inp["claude_model"], "SETTINGS": str(settings),
                  "LIMITS": (REPO_ROOT / "ci" / "review-limits.md").read_text(), "OUT": str(rd / "highlights-raw.md"), "PY": py}
        hrc, _ = job.sh(["bash", "-c",
                         'set -o pipefail; claude -p "$PROMPT" --model "$MODEL" --output-format stream-json --verbose '
                         '--permission-mode dontAsk --permission-prompts none --settings "$SETTINGS" '
                         '--append-system-prompt "$LIMITS" --allowedTools Read Glob Grep '
                         '| "$PY" ci/agent-stream.py "$OUT"'], hl_env, timeout=600)
        if hrc == 0:
            job.sh([py, "ci/highlights.py", str(rd), str(rd / "highlights-raw.md")], env)
        _render(job, rd)
        job.stage("highlights", "passed" if hrc == 0 and (rd / "highlights.json").exists() else "failed")

    if job.cancelled:
        return
    meta = read_json(rd / "run.json", {}) if rd else {}
    job.update(status=(meta or {}).get("status") or ("passed" if rc == 0 else "failed"))
    if inp["post_comment"] and rd and job.data.get("pr"):
        post_comment(job, None)


def _render(job: Job, rd: Path) -> None:
    from .report import Run, render_html
    try:
        render_html(Run(rd))
    except (HarnessError, OSError, ValueError) as e:
        job.note(f"report.html not rendered: {e}")


def post_comment(job: Job, artifact_url: str | None) -> None:
    """`th report pr` with this Mac's gh login; the screenshots go up with `gh pr comment --attach`."""
    rd = job.run_dir()
    job.update(comment={"status": "posting", "at": _now()})
    cmd = ["./trio-test-harness", "report", "pr", job.data["pr"], "--run", str(rd)]
    if artifact_url:
        cmd += ["--artifact-url", artifact_url]
    job.note("$ " + " ".join(cmd))
    try:
        cp = subprocess.run(cmd, cwd=REPO_ROOT, env=_base_env(), capture_output=True, text=True, timeout=900)
        with open(job.log_path, "a") as fh:
            fh.write(cp.stdout + cp.stderr)
        if cp.returncode == 0:
            url = next((l for l in reversed(cp.stdout.splitlines()) if l.startswith("https://")), "")
            job.update(comment={"status": "posted", "url": url, "at": _now()})
        else:
            job.update(comment={"status": "failed", "error": (cp.stderr or cp.stdout)[-1500:], "at": _now()})
    except subprocess.TimeoutExpired:
        job.update(comment={"status": "failed", "error": "gh did not finish within 15 minutes", "at": _now()})


# ---------------- options for the form -----------------

def _xcodes() -> list[str]:
    return sorted(str(p) for p in Path("/Applications").glob("Xcode*.app"))


_OPTIONS: dict | None = None


def options() -> dict:
    global _OPTIONS
    if _OPTIONS is None:
        from . import xcode as X
        o = {"xcodes": _xcodes(), "sim_devices": [], "sim_runtimes": [],
             "defaults": {"xcode": CONFIG.get("TH_XCODE_APP"), "sim_device": CONFIG.get("TH_SIM_DEVICE"),
                          "sim_runtime": CONFIG.get("TH_SIM_RUNTIME")}}
        try:
            o["sim_devices"] = [d["name"] for d in X.list_devicetypes() if d.get("name", "").startswith("iPhone")]
            o["sim_runtimes"] = [r["name"] for r in X.list_runtimes()]
        except Exception as e:  # noqa: BLE001  (no Xcode yet: free-text fields still work)
            log(f"webui: simulator options unavailable: {e}")
        _OPTIONS = o
    return _OPTIONS


# ---------------- HTTP -----------------

def _token() -> str:
    if TOKEN_FILE.exists() and (t := TOKEN_FILE.read_text().strip()):
        return t
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    t = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(t)
    TOKEN_FILE.chmod(0o600)
    return t


class Handler(BaseHTTPRequestHandler):
    server_version = "trio-test-webui"
    manager: Manager
    token: str

    def log_message(self, fmt, *args):   # quiet: only errors
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            log("webui: " + fmt % args)

    # --- helpers ---
    def _send(self, code: int, body: bytes | str, ctype: str = "application/json", headers: dict | None = None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, default=str))

    def _authed(self) -> bool:
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return COOKIE in c and secrets.compare_digest(c[COOKIE].value, self.token)

    def _job(self, jid: str) -> Job | None:
        return self.manager.jobs.get(jid)

    # --- live phone (rooted-vnc) ---
    def _vnc_ws(self):
        from . import liveview as L
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return self._send(403, "cross-origin WebSocket", "text/plain")
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or self.headers.get("Upgrade", "").lower() != "websocket":
            return self._send(400, "WebSocket upgrade expected", "text/plain")
        self.protocol_version = "HTTP/1.1"      # browsers reject a 101 on HTTP/1.0
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", L.accept_key(key))
        if "binary" in [p.strip() for p in self.headers.get("Sec-WebSocket-Protocol", "").split(",")]:
            self.send_header("Sec-WebSocket-Protocol", "binary")
        self.end_headers()
        self.close_connection = True
        L.proxy(L.WebSocket(self.rfile, self.wfile))

    def _device(self, action: str, body: dict):
        """Harness buttons for the phone. Screenshots go into the job's run when a finished job is open, else into
        runs/.webui/screens/."""
        from . import liveview as L
        try:
            if action == "shot":
                job = self._job(str(body.get("job") or ""))
                rd = job.run_dir() if job and job.data.get("status") not in ACTIVE else None
                if rd:
                    from .report import Run
                    run = Run(rd)
                    path = run.shot_path("manual")
                    L.act(lambda d: d.screenshot(path))
                    note = str(body.get("note") or "").strip()[:300] or "Screenshot taken from the web UI"
                    run.log_step("manual", note, path)
                    _render(job, rd)
                    return self._json({"message": f"added to {rd.name} and report.html",
                                       "url": f"/jobs/{job.id}/screenshots/{path.name}"})
                SCREENS_DIR.mkdir(parents=True, exist_ok=True)
                path = SCREENS_DIR / f"{dt.datetime.now():%Y%m%d-%H%M%S}.png"
                L.act(lambda d: d.screenshot(path))
                return self._json({"message": f"saved {path.relative_to(REPO_ROOT)}", "url": f"/api/device/screens/{path.name}"})
            if action == "type":
                text = str(body.get("text") or "")[:500]
                if not text:
                    return self._json({"error": "nothing to type"}, 400)
                L.act(lambda d: d.type_text(text))
                return self._json({"message": f"typed {len(text)} characters"})
            if action == "launch":
                from . import devices as Dv
                from .build import BuildInfo
                bi = BuildInfo.load()
                bundle = bi.bundle_id if bi and str(bi.target).startswith("device") else CONFIG.bundle_id("rooted-vnc")
                Dv.ssh_launch(bundle)
                return self._json({"message": f"launched {bundle}"})
            simple = {"home": ("home", "Home pressed"), "lock": ("power", "power pressed"),
                      "unlock": ("unlock", "unlocked"), "wake": ("wake", "woken")}
            if action in simple:
                method, msg = simple[action]
                res = L.act(lambda d: getattr(d, method)())
                if action == "unlock" and res is False:
                    return self._json({"error": "still locked (passcode screen showing); try again"}, 502)
                return self._json({"message": msg})
        except (HarnessError, OSError) as e:
            return self._json({"error": str(e)[:600]}, 502)
        return self._json({"error": f"unknown action {action}"}, 404)

    def _applogs(self, q: dict):
        """New bytes of the installed app's Trio or TandemKit log (the log pane polls this; harness/applogs.py)."""
        from . import applogs as A
        which = (q.get("which") or ["trio"])[0]
        rel = {"trio": "logs/log.txt", "tandem": "tandem/tandem_log.txt"}.get(which)
        target = TARGETS.get((q.get("target") or ["rooted-vnc"])[0])
        if not rel or not target:
            return self._json({"error": "which: trio|tandem, target: rooted-vnc|simulator"}, 400)
        off = (q.get("off") or [""])[0]
        try:
            res = A.read_tail(target, A.default_bundle(target), rel, int(off) if off.isdigit() else None)
        except (HarnessError, OSError) as e:
            return self._json({"error": str(e)[-600:]}, 502)
        return self._json(res)

    def _continue(self, job: Job, body: dict):
        """Carry a finished run on (harness/handoff.py): local | cloud | followup."""
        rd = job.run_dir()
        if job.data.get("status") in ACTIVE:
            return self._json({"error": "wait for the run to finish"}, 409)
        if not rd:
            return self._json({"error": "this job has no run"}, 409)
        kind = str(body.get("kind") or "")
        env = _base_env()
        try:
            if kind == "local":
                mode = str(body.get("permission_mode") or "auto")
                model = str(body.get("model") or job.data["inputs"].get("claude_model") or "")
                if model and not MODEL.match(model):
                    return self._json({"error": "model: letters, digits and . : - [ ] only"}, 400)
                meta = job.continuations.start_local(job.data, rd, env, copy=bool(body.get("copy", True)),
                                                     mode=mode, model=model)
            elif kind == "cloud":
                meta = job.continuations.start_cloud(job.data, rd, env)
            elif kind == "followup":
                from . import handoff as H
                session = str(body.get("session") or H.pr_session(rd) or "")
                meta = job.continuations.send_followup(job.data, rd, env, session)
            else:
                return self._json({"error": "kind must be local, cloud or followup"}, 400)
        except HarnessError as e:
            return self._json({"error": str(e)}, 409)
        job.note(f"continue: {kind} #{meta['id']} started")
        return self._json(meta, 201)

    # --- routes ---
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/" and "token" in q:
            if secrets.compare_digest(q["token"][0], self.token):
                return self._send(303, "", "text/plain", {
                    "Location": "/", "Set-Cookie": f"{COOKIE}={self.token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"})
            return self._send(403, "wrong token", "text/plain")
        if not self._authed():
            return self._send(403, "Open the URL that `trio-test-harness webui` printed (it carries ?token=...).",
                              "text/plain")
        parts = [p for p in u.path.split("/") if p]
        if u.path == "/":
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if parts == ["api", "state"]:
            jobs = sorted(self.manager.jobs.values(), key=lambda j: j.id, reverse=True)
            return self._json({"jobs": [j.public() for j in jobs[:100]]})
        if parts == ["api", "options"]:
            return self._json(options())
        if parts == ["api", "applogs"]:
            return self._applogs(q)
        if parts == ["api", "vnc", "ws"]:
            return self._vnc_ws()
        if parts == ["api", "vnc", "status"]:
            from . import daemon as D
            from . import liveview as L
            return self._json({"daemon": D.is_running("rooted-vnc"), "last_error": L.last_error})
        if len(parts) == 4 and parts[:3] == ["api", "device", "screens"] and SHOT_NAME.match(parts[3]):
            f = SCREENS_DIR / parts[3]
            if f.is_file():
                return self._send(200, f.read_bytes(), "image/png")
        if len(parts) >= 3 and parts[:2] == ["api", "jobs"] and (job := self._job(parts[2])):
            if len(parts) == 3:
                return self._json(job.public(full=True))
            if parts[3] == "log.txt":          # the whole log, for the Copy button
                data = job.log_path.read_bytes() if job.log_path.exists() else b""
                return self._send(200, data, "text/plain; charset=utf-8")
            if parts[3] == "log":
                asked = int((q.get("offset") or ["0"])[0] or 0)
                size = job.log_path.stat().st_size if job.log_path.exists() else 0
                off = max(min(asked, size), size - 512 * 1024)     # at most 512 KB per request
                with open(job.log_path, "rb") as fh:
                    fh.seek(off)
                    text = fh.read().decode(errors="replace")
                return self._json({"offset": size, "text": text, "skipped": off - min(asked, size)})
            if parts[3] == "preview":
                rd = job.run_dir()
                if not rd:
                    return self._json({"error": "no run directory"}, 404)
                from .report import Run, render_markdown
                md, files = render_markdown(Run(rd))
                return self._json({"markdown": md, "screenshots": [str(f.relative_to(rd)) for f in files]})
        if len(parts) == 3 and parts[0] == "jobs" and (job := self._job(parts[1])) and (rd := job.run_dir()):
            f = (rd / parts[2]).resolve()
            if parts[2] == "report.html" and f.is_file():
                return self._send(200, f.read_bytes(), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'"})
        if (len(parts) == 4 and parts[0] == "jobs" and parts[2] == "screenshots" and SHOT_NAME.match(parts[3])
                and (job := self._job(parts[1])) and (rd := job.run_dir()) and (rd / "screenshots" / parts[3]).is_file()):
            return self._send(200, (rd / "screenshots" / parts[3]).read_bytes(), "image/png")
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        if not self._authed():
            return self._send(403, "forbidden", "text/plain")
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._send(415, "application/json only", "text/plain")   # no cross-site form posts
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)) or b"{}")
        except ValueError:
            return self._json({"error": "bad JSON"}, 400)
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if parts == ["api", "jobs"]:
            try:
                job = self.manager.submit(body)
            except ValueError as e:     # includes ci/inputs.py's InputError
                return self._json({"error": str(e)}, 400)
            return self._json(job.public(), 201)
        if len(parts) == 3 and parts[:2] == ["api", "device"]:
            return self._device(parts[2], body)
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and (job := self._job(parts[2])):
            if parts[3] == "cancel":
                if job.data.get("status") not in ACTIVE:
                    return self._json({"error": "not running"}, 409)
                job.cancel()
                if job.data.get("status") in ("queued", "waiting"):
                    job.update(status="cancelled")
                return self._json(job.public())
            if parts[3] == "comment":
                if not job.data.get("pr"):
                    return self._json({"error": "this run has no PR (Trio branch only)"}, 409)
                if not job.run_dir():
                    return self._json({"error": "this job has no run to report"}, 409)
                if job.data.get("status") in ACTIVE:
                    return self._json({"error": "wait for the run to finish"}, 409)
                if (job.data.get("comment") or {}).get("status") == "posting":
                    return self._json({"error": "already posting"}, 409)
                url = str(body.get("artifact_url") or "").strip() or None
                if url and not re.match(r"^https://[^\s<>\"]+$", url):
                    return self._json({"error": "report link must be an https:// URL"}, 400)
                job.update(comment={"status": "posting", "at": _now()})
                threading.Thread(target=post_comment, args=(job, url), daemon=True).start()
                return self._json(job.public())
            if parts[3] == "continue":
                return self._continue(job, body)
        if (len(parts) == 6 and parts[:2] == ["api", "jobs"] and parts[3] == "continue" and parts[5] in ("stop", "remove-copy")
                and (job := self._job(parts[2]))):
            try:
                if parts[5] == "stop":
                    return self._json(job.continuations.stop(parts[4]))
                return self._json(job.continuations.remove_copy(parts[4]))
            except HarnessError as e:
                return self._json({"error": str(e)}, 409)
        return self._send(404, "not found", "text/plain")


def serve(host: str = "127.0.0.1", port: int = 8787, open_browser: bool = False, token: str | None = None) -> None:
    if token is not None and not re.match(r"^[A-Za-z0-9._~-]+$", token):
        raise HarnessError("--token: use letters, digits and . _ ~ - only (it goes into a URL and a cookie)")
    if token is not None and len(token) < 12:
        log("webui: that token is short; anyone who can reach the page could guess it")
    Handler.manager = Manager()
    Handler.token = token or _token()
    server_cls = ThreadingHTTPServer
    if ":" in host:                     # IPv6 bind address, e.g. "::"
        server_cls = type("ThreadingHTTPServerV6", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6})
    httpd = server_cls((host, port), Handler)
    local = host in ("127.0.0.1", "localhost", "::1")
    shown = [lan_ip(), "127.0.0.1"] if host in ("0.0.0.0", "", "::") else [f"[{host}]" if ":" in host else host]
    url = f"http://{shown[-1]}:{port}/?token={Handler.token}"
    if not local:
        log(f"webui: listening on {host or '0.0.0.0'}:{port}, reachable from other machines; anyone with the token URL "
            "can build, drive the phone and comment on PRs as you")
    for h in shown:
        print(f"trio-test-harness webui: http://{h}:{port}/?token={Handler.token}", flush=True)
    if open_browser:
        subprocess.run(["open", url], check=False)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for job in Handler.manager.jobs.values():
            if job.data.get("status") in ACTIVE:
                job.cancel()
        from . import liveview
        liveview.close()

