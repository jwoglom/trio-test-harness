"""faketandem, the Tandem pump emulator (https://github.com/jwoglom/faketandem), as the app's pump.

`--pump faketandem` builds the app with TANDEMKIT_VIRTUAL_PUMP (a Swift compilation condition, Debug builds only):
TandemKit's stand-in for CoreBluetooth (TandemKit Sources/TandemKit/PumpManager/VirtualPump) then reaches the pump
over a TCP socket instead of the radio, at the host:port in TANDEMKIT_VIRTUAL_PUMP in the app's environment. That is
how TandemKit's own TrioIntegrationTests and the tandemkit-stress lane run Trio against faketandem.

One faketandem runs per checkout, in the background, so it outlives the command that started it (trio-test-e2e, then
the agent). Its pid, ports and setup are in runs/.tmp/faketandem.json, and every simulator launch
(`SimulatorDriver.launch`) passes the app its address. An app started any other way, such as a tap on its icon after
it was stopped, has none and reports Bluetooth as unsupported.

Two ways to begin:
  walk     the pump waits to be paired, discoverable at the "double tap" step: someone (the agent) adds it in the app,
           moves it on to show its code (`th faketandem pairing PairStep2`) and enters PAIRING_CODE.
  preseed  the pump is already paired: faketandem holds a long-term key and the app's PumpManagerState.plist holds
           the driver's state after a pairing with it, as TandemKit's TrioClosedLoopTests seed it.
A scenario (faketandem's -scenario: a preset such as recent-doses, or a JSON file) gives the pump hours of use before
the app first connects: history, IOB, a last bolus, a running temp basal.

faketandem's HTTP API (state, pump-side actions, history, faults) is reached with `th faketandem api`, through the sim
daemon when one runs: the agent's sandbox has no network.
"""
from __future__ import annotations

import json
import os
import platform
import plistlib
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

from .config import CONFIG, REPO_ROOT
from .util import HarnessError, log, read_json, run, which, write_json

STATE_FILE = REPO_ROOT / "runs" / ".tmp" / "faketandem.json"
LOG_FILE = REPO_ROOT / "runs" / ".tmp" / "faketandem.log"
HOST = "127.0.0.1"
PAIRING_CODE = "123456"          # faketandem's default; changing it drops a seeded long-term key
SETUPS = ("preseed", "walk")
# The scan reports a pump only once it advertises a pairing step: PairStep1 is "double tap the pump" (0x11 in its
# manufacturer data), PairStep2 "enter the code" (0x12). TandemKit's scan view model moves through both.
PAIRING_STATES = ("NotDiscoverable", "PairStep1", "PairStep2")
WALK_PAIRING_STATE = "PairStep1"
# A paired pump stays discoverable: the virtual central does not connect to one that is not advertising.
PAIRED_PAIRING_STATE = "PairStep2"
# faketandem's default profile is one segment at this rate; the app's schedule is seeded to match, so neither the
# driver's basal check nor the history reconciliation finds a difference the harness made.
PUMP_BASAL_RATE = 0.85
MAX_BASAL = 2.0
MAX_BOLUS = 10.0
TANDEM_PLUGIN_ID = "TandemPumpManager"
REMOTE = {"faketandem_api"}


# ---------------- the binary -----------------

def _git(args: list[str], cwd: Path, **kw):
    """Anonymously first (faketandem is public), then with the harness's GitHub credentials for a private fork."""
    from .build import git_env
    cp = run(["git", *args], cwd=cwd, env={"GIT_TERMINAL_PROMPT": "0"}, check=False, quiet=True, **kw)
    if cp.returncode == 0:
        return cp
    return run(["git", *args], cwd=cwd, env=git_env(), quiet=True, **kw)


def binary(ref: str | None = None) -> tuple[Path, dict]:
    """faketandem to run: TH_FAKETANDEM_BIN, else built with Go from TH_FAKETANDEM_DIR or from TH_FAKETANDEM_REPO at
    `ref` (TH_FAKETANDEM_REF, main), else without Go the `latest` release binary (main only)."""
    if CONFIG.get("TH_FAKETANDEM_BIN"):
        path = Path(CONFIG.get("TH_FAKETANDEM_BIN")).expanduser()
        if not path.exists():
            raise HarnessError(f"TH_FAKETANDEM_BIN={path} does not exist")
        return path, {"source": str(path)}
    ref = ref or CONFIG.get("TH_FAKETANDEM_REF") or "main"
    out = CONFIG.build_dir / "faketandem-bin" / "faketandem"
    if not which("go"):
        if ref not in ("main", "latest"):
            raise HarnessError(f"faketandem {ref} has to be built and Go is not installed (brew install go)")
        return _download_latest(out), {"source": "release latest"}
    if CONFIG.get("TH_FAKETANDEM_DIR"):
        src = Path(CONFIG.get("TH_FAKETANDEM_DIR")).expanduser().resolve()
        if not (src / "go.mod").exists():
            raise HarnessError(f"TH_FAKETANDEM_DIR={src} is not a faketandem checkout")
        info = {"source": str(src)}
    else:
        src = CONFIG.build_dir / "faketandem-src"
        url = f"https://github.com/{CONFIG.get('TH_FAKETANDEM_REPO') or 'jwoglom/faketandem'}.git"
        if not (src / ".git").exists():
            src.mkdir(parents=True, exist_ok=True)
            _git(["init", "--quiet"], src)
        log(f"fetching faketandem {ref} from {url}")
        _git(["fetch", "--quiet", "--depth", "1", url, ref], src, timeout=900)
        _git(["checkout", "--quiet", "--force", "--detach", "FETCH_HEAD"], src)
        info = {"source": url, "ref": ref}
    info["sha"] = run(["git", "rev-parse", "--short=10", "HEAD"], cwd=src, check=False, quiet=True).stdout.strip()
    out.parent.mkdir(parents=True, exist_ok=True)
    log(f"building faketandem {info.get('sha') or ''} from {src}")
    run(["go", "build", "-o", str(out), "."], cwd=src, timeout=900, quiet=True)
    return out, info


def _download_latest(out: Path) -> Path:
    import requests
    arch = "arm64" if platform.machine() in ("arm64", "aarch64") else "amd64"
    system = "darwin" if platform.system() == "Darwin" else "linux"
    url = f"https://github.com/jwoglom/faketandem/releases/download/latest/faketandem-latest-{system}-{arch}"
    log(f"downloading {url}")
    r = requests.get(url, timeout=300)
    if r.status_code != 200:
        raise HarnessError(f"{url}: HTTP {r.status_code}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(r.content)
    out.chmod(0o755)
    return out


# ---------------- the process -----------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:      # this process started it, and it has exited
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def current() -> dict | None:
    """The running faketandem's record, None when none runs."""
    try:
        state = read_json(STATE_FILE)
    except ValueError:
        return None
    return state if state and _alive(state.get("pid")) else None


def launch_env() -> dict[str, str]:
    """What the app needs in its environment to reach the running faketandem; empty when none runs."""
    state = current()
    return {"TANDEMKIT_VIRTUAL_PUMP": state["gatt"]} if state else {}


def read_hello(gatt: str, timeout: float = 5.0) -> dict:
    """The `hello` the virtual peripheral sends on every connection: its peripheral_id, name and pairing state.
    Closing without `attach` is what TandemKit's virtual scan does too."""
    host, port = gatt.rsplit(":", 1)
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
        s.settimeout(timeout)
        line = s.makefile("r", encoding="utf-8").readline()
    try:
        hello = json.loads(line)
    except ValueError as e:
        raise HarnessError(f"faketandem {gatt} sent no hello: {line[:200]!r}") from e
    if hello.get("type") != "hello" or not hello.get("peripheral_id"):
        raise HarnessError(f"faketandem {gatt}: unexpected first frame {line[:200]!r}")
    return hello


def start(*, setup: str = "preseed", scenario: str | None = None, ref: str | None = None,
          startup_timeout: float = 60.0) -> dict:
    """A new faketandem (any running one is stopped) for `setup`, with `scenario` replayed before anything connects."""
    if setup not in SETUPS:
        raise HarnessError(f"unknown pump setup {setup!r} ({' | '.join(SETUPS)})")
    stop()
    path, info = binary(ref)
    gatt, api_port = f"{HOST}:{_free_port()}", _free_port()
    cmd = [str(path), "-transport", "virtual", "-virtual-addr", gatt, "-api-addr", f"{HOST}:{api_port}", "-q"]
    key = os.urandom(32) if setup == "preseed" else None
    if key:
        cmd += ["-jpake-long-term-key", key.hex()]
    if scenario:
        cmd += ["-scenario", str(Path(scenario).resolve()) if Path(scenario).is_file() else scenario]
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log(f"starting faketandem ({setup}{', scenario ' + scenario if scenario else ''}): GATT {gatt}, "
        f"API http://{HOST}:{api_port}, log {LOG_FILE}")
    with open(LOG_FILE, "w") as fh:
        # its own session, so it outlives this command and a stop takes its whole group
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=str(path.parent), start_new_session=True)
    state = {"pid": proc.pid, "gatt": gatt, "api": f"http://{HOST}:{api_port}", "log": str(LOG_FILE),
             "setup": setup, "scenario": scenario or "", "pairing_code": PAIRING_CODE, "binary": str(path), **info,
             "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    deadline = time.time() + startup_timeout
    while True:
        if proc.poll() is not None:
            raise HarnessError(f"faketandem exited during startup ({proc.returncode}):\n{_tail()}")
        try:
            _http(state["api"], "GET", "/api/bluetooth/pairingstate", timeout=2)
            break
        except HarnessError:
            if time.time() > deadline:
                os.killpg(proc.pid, signal.SIGKILL)
                raise HarnessError(f"faketandem's API did not come up in {startup_timeout:.0f}s:\n{_tail()}")
            time.sleep(0.5)
    if key:
        state["long_term_key"] = key.hex()
    write_json(STATE_FILE, state)
    set_pairing_state(PAIRED_PAIRING_STATE if setup == "preseed" else WALK_PAIRING_STATE)
    hello = read_hello(gatt)
    state.update(peripheral_id=str(hello["peripheral_id"]).upper(), name=hello.get("name") or "")
    write_json(STATE_FILE, state)
    return state


def _tail(n: int = 30) -> str:
    try:
        return "\n".join(LOG_FILE.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def stop(save_to: Path | None = None) -> bool:
    """Stop the running faketandem; with `save_to` (a run directory), first keep its state, history and request log
    there, and its log. Returns whether one was running."""
    state = current()
    if state and save_to:
        save(save_to)
    if state:
        try:
            os.killpg(state["pid"], signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        for _ in range(20):
            if not _alive(state["pid"]):
                break
            time.sleep(0.25)
        else:
            try:
                os.killpg(state["pid"], signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        log(f"stopped faketandem (pid {state['pid']})")
    STATE_FILE.unlink(missing_ok=True)
    return bool(state)


def save(run_dir: Path) -> list[Path]:
    """The pump's side of a run, into its directory: faketandem/state.json, history.json, requests.json and the log."""
    out = Path(run_dir) / "faketandem"
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, path in (("state.json", "/api/state"), ("history.json", "/api/history?limit=5000"),
                       ("requests.json", "/api/log")):
        try:
            (out / name).write_text(json.dumps(api("GET", path), indent=2))
            written.append(out / name)
        except HarnessError as e:
            log(f"faketandem {path} not saved: {e}")
    if LOG_FILE.exists():
        shutil.copy2(LOG_FILE, out / "faketandem.log")
        written.append(out / "faketandem.log")
    return written


# ---------------- the API -----------------

def _http(base: str, method: str, path: str, body=None, timeout: float = 30):
    import requests
    try:
        r = requests.request(method, base + path, json=body, timeout=timeout)
    except requests.RequestException as e:
        raise HarnessError(f"faketandem {method} {path}: {e}") from e
    if r.status_code >= 400:
        raise HarnessError(f"faketandem {method} {path} -> {r.status_code}: {r.text[:500]}")
    if not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return r.text


def faketandem_api(target: str, method: str, path: str, body=None):
    """One request to the running faketandem (REMOTE: the daemon makes it for a sandboxed caller)."""
    state = current()
    if not state:
        raise HarnessError("no faketandem is running (`th faketandem start`, or trio-test-e2e --pump faketandem)")
    return _http(state["api"], method.upper(), path if path.startswith("/") else "/" + path, body)


def api(method: str, path: str, body=None):
    """faketandem_api here, or in the sim daemon when one runs: the agent's sandbox can reach only its socket."""
    from . import daemon as Dm
    if Dm.is_running("sim"):
        try:
            return Dm.RemoteDriver("sim", timeout=120)._call("faketandem_api", method, path, body)
        except HarnessError as e:
            if "faketandem_api" in str(e) and "AttributeError" in str(e):
                raise HarnessError("the running sim daemon predates `th faketandem`; restart it: "
                                   "th --target sim daemon stop, then daemon start") from None
            raise
    return faketandem_api("sim", method, path, body)


def set_pairing_state(state: str) -> str:
    if state not in PAIRING_STATES:
        raise HarnessError(f"unknown pairing state {state!r} ({' | '.join(PAIRING_STATES)})")
    api("POST", "/api/bluetooth/pairingstate", {"pairingState": state})
    return api("GET", "/api/bluetooth/pairingstate")["pairingState"]


def summary(snapshot: dict | None = None) -> list[str]:
    """What the pump holds, as a person reading its screen would put it."""
    s = snapshot or api("GET", "/api/state")
    basal, insulin, last = s.get("basal") or {}, s.get("insulin") or {}, s.get("last_bolus")
    auth, conn = s.get("auth") or {}, s.get("connection") or {}
    lines = [f"pairing: {auth.get('pairing_state')}, code {auth.get('pairing_code')}, "
             f"{'a long-term key held' if auth.get('long_term_key_present') else 'no long-term key'}; "
             f"{'connected' if conn.get('connected') else 'not connected'}"
             + (", authenticated" if auth.get("authenticated") else "")]
    if basal.get("suspended"):
        lines.append("basal: suspended")
    elif basal.get("temp_active"):
        lines.append(f"basal: temp {basal.get('temp_percent')}% = {basal.get('temp_rate'):.3f} U/hr until "
                     f"{basal.get('temp_end')} (profile {basal.get('profile_rate'):.3f} U/hr)")
    else:
        lines.append(f"basal: profile {basal.get('profile_rate', 0):.3f} U/hr")
    lines.append(f"IOB shown on the pump: {insulin.get('iob', 0):.2f} U"
                 + (" (Control-IQ)" if insulin.get("iob_type") == 1 else " (bolus insulin, Control-IQ off)"))
    if last:
        lines.append(f"last bolus: #{last.get('bolus_id')} {last.get('delivered_units', 0):.2f} of "
                     f"{last.get('requested_units', 0):.2f} U, ended {last.get('end_time')} "
                     f"(end reason {last.get('end_reason_id')}, source {last.get('source_id')})")
    lines.append(f"reservoir {insulin.get('reservoir_units', 0):.1f} U; history log {s.get('history', {}).get('count', 0)} "
                 "records")
    return lines


def history_doses(limit: int = 5000) -> list[str]:
    """The history log's dose records, oldest first (the 5-minute BasalDelivery/DailyBasal records left out)."""
    page = api("GET", f"/api/history?limit={limit}")
    skip = {"BasalDelivery", "DailyBasal", "ControlIQPcmChange"}
    return [f"#{e['sequence']} {e['time'][:19]}Z {e['type']} {json.dumps(e.get('data') or {}, sort_keys=True)}"
            for e in page.get("entries", []) if e.get("type") not in skip]


# ---------------- the app's side -----------------

def host_time_zone() -> str:
    """IANA name of this Mac's zone (the simulator's), for TandemKit's basal schedule; UTC when it cannot be told."""
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return os.environ.get("TZ") or "UTC"


def paired_pump_manager_state(state: dict | None = None) -> dict:
    """PumpManagerState.plist for the app (Trio and Loop restore their pump manager from it): a TandemPumpManager
    after a pairing with the running faketandem. A restored driver holding the pump's long-term key authenticates
    through JPAKE rounds 3/4 alone, and finds the pump by `peripheralUuid`, compared upper case."""
    state = state or current()
    if not state or not state.get("long_term_key"):
        raise HarnessError("no faketandem started for a pre-seeded pump (`th faketandem start --setup preseed`)")
    return {
        "managerIdentifier": TANDEM_PLUGIN_ID,
        "state": {
            "onboarded": True,
            "pumpState": {
                "peripheralUuid": state["peripheral_id"].upper(),
                "derivedSecret": bytes.fromhex(state["long_term_key"]),
                "pincode": state.get("pairing_code") or PAIRING_CODE,
            },
            "hasSyncedBasalRateSchedule": True,
            "basalRateSchedule": {"items": [{"startTime": 0.0, "value": PUMP_BASAL_RATE}],
                                  "timeZoneIdentifier": host_time_zone()},
            "settings": {"maxBolus": MAX_BOLUS, "maxTempBasalRate": MAX_BASAL},
        },
    }


def write_pump_manager_state(container: Path, raw: dict) -> Path:
    path = container / "Documents" / "PumpManagerState.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(raw, fmt=plistlib.FMT_BINARY))
    return path


def agent_notes(run_dir: Path) -> str:
    """The CI / web UI agent's instructions for a run's faketandem pump (ci/agent-faketandem*.md); empty without one."""
    try:
        pump = json.loads((Path(run_dir) / "run.json").read_text()).get("pump") or {}
    except (OSError, ValueError):
        return ""
    if pump.get("kind") != "faketandem":
        return ""
    ci = REPO_ROOT / "ci"
    return "\n" + (ci / "agent-faketandem.md").read_text() + "\n" + \
        (ci / f"agent-faketandem-{pump.get('setup') or 'preseed'}.md").read_text()
