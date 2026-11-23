"""Make the app under test believe it is another build and hear another version-check answer.

Trio's AppVersionChecker (Trio/Sources/Services/AppVersionChecker/AppVersionChecker.swift) fetches
blacklisted-versions.json and Config.xcconfig from raw.githubusercontent.com and compares them with the running
build's CFBundleShortVersionString and, since nightscout/Trio#1798, the branch and commit SHA in BuildDetails.plist.
To test that against any build and any blacklist, `th versionmock set` loads a small shim into the app
(harness/shims/THVersionMock.m) that answers those requests from fixture files and overrides those values, with no
change to the app or its build:

- simulator: every `simctl launch` of the app (th launch, the daemon's, the pipeline's, `db exec`'s relaunch) gets
  SIMCTL_CHILD_DYLD_INSERT_LIBRARIES while a mock is set, and the config is copied into the app's data container;
- rooted-vnc: the shim goes in as a MobileSubstrate/ElleKit library filtered to the app's bundle id (inert without
  its config), the config into the app's data container over SSH.

The shim also removes the checker's cached answers and "shown in the last 24 hours" stamps once per `set` (or at every
launch with --reset-every-launch): the checker otherwise reuses a 24-hour-old answer and shows the critical alert at
most once a day. It logs what it served to log.jsonl in the container (`th versionmock log`).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from .config import REPO_ROOT
from .util import HarnessError, log, read_json, run, write_json

SHIM_SOURCE = REPO_ROOT / "harness" / "shims" / "THVersionMock.m"
WORK_DIR = REPO_ROOT / "runs" / ".tmp" / "versionmock"
ACTIVE_DIR = WORK_DIR / "active"
STATE_FILE = REPO_ROOT / "runs" / ".tmp" / "versionmock.json"
CONTAINER_SUBDIR = "Library/th-versionmock"        # in the app's data container; the shim's default config place
PHONE_TWEAK_DIR = "/var/jb/Library/MobileSubstrate/DynamicLibraries"
PHONE_TWEAK_NAME = "THVersionMock"

# What Trio fetches (AppVersionChecker.GitHubDataType).
URLS = {
    "blacklist": "https://raw.githubusercontent.com/nightscout/Trio/refs/heads/main/blacklisted-versions.json",
    "config": "https://raw.githubusercontent.com/nightscout/Trio/refs/heads/main/Config.xcconfig",
    "dev-config": "https://raw.githubusercontent.com/nightscout/Trio/refs/heads/dev/Config.xcconfig",
}

# AppVersionChecker's @Persisted keys (UserDefaults.standard): its cached answer and when each alert was last shown.
# lastExpirationNotificationShown is left alone: it belongs to the build-expiry warning.
CACHE_KEYS = [
    "cachedForVersion", "latestVersion", "latestVersionChecked", "currentVersionBlackListed",
    "currentVersionBlacklistMessage", "cachedForCommitSHA", "lastBlacklistNotificationShown",
    "lastVersionUpdateNotificationShown", "cachedForDevVersion", "latestDevVersion", "latestDevVersionChecked",
]

# BuildDetails.plist keys (Trio/Sources/Helpers/BuildDetails.swift, written by scripts/capture-build-details.sh).
BRANCH_KEY = "com-trio-branch"
SHA_KEY = "com-trio-commit-sha"
BUILD_DETAILS = "BuildDetails.plist"

ALERT_TITLES = {"Update Required": "update-required", "Update Available": "update-available"}
BLACKLIST_TEXT = "The current version has a critical issue and should be updated as soon as possible."
AVAILABLE_TEXT = "is available. It is recommended to update."
SETTINGS_WARNING = "Warning: Known issues. Update now."
ALERT_KINDS = ("update-required", "update-available", "none")


# ---------------- the config the shim reads -----------------

def blacklist_json(entries: list[dict]) -> str:
    return json.dumps({"blacklistedVersions": entries}, indent=2) + "\n"


def xcconfig(version: str, dev_version: str | None = None) -> str:
    """The lines of Config.xcconfig that AppVersionChecker parses (APP_VERSION, APP_DEV_VERSION)."""
    return f"APP_VERSION = {version}\nAPP_DEV_VERSION = {dev_version or version}\n"


def build_config(spec: dict, token: str | None = None) -> tuple[dict, dict[str, str]]:
    """The shim's config.json and the body files it names ({relative path: text}) for a spec:

    version, build_number, branch, sha          the build the app reports ("" for branch/sha: not recorded)
    build_details, info                          more BuildDetails.plist / Info.plist keys (None removes one)
    blacklist                                    the blacklist file's text (None: the real one is fetched)
    blacklist_status                             HTTP status for it (default 200)
    latest, latest_dev                           APP_VERSION of main's / dev's Config.xcconfig (None: real one)
    fail                                         names from URLS that fail with a network error instead
    routes                                       more [{"url", "body" | "body_file", "status", "error", "delay"}]
    reset_every_launch                           remove CACHE_KEYS at every launch, not once per token
    """
    info = dict(spec.get("info") or {})
    if spec.get("version"):
        info["CFBundleShortVersionString"] = spec["version"]
    if spec.get("build_number"):
        info["CFBundleVersion"] = spec["build_number"]
    details = dict(spec.get("build_details") or {})
    for field, key in (("branch", BRANCH_KEY), ("sha", SHA_KEY)):
        if spec.get(field) is not None:
            details[key] = spec[field] or None          # "" = the build recorded none
    files: dict[str, str] = {}
    routes: list[dict] = []
    fail = set(spec.get("fail") or [])
    unknown = fail - set(URLS)
    if unknown:
        raise HarnessError(f"versionmock: --fail takes {', '.join(URLS)}, not {', '.join(sorted(unknown))}")

    def add(name: str, body: str | None, status: int = 200):
        if name in fail:
            routes.append({"url": URLS[name], "error": "offline"})
        elif body is not None:
            rel = f"bodies/{name}"
            files[rel] = body
            routes.append({"url": URLS[name], "status": status, "body_file": rel})

    add("blacklist", spec.get("blacklist"), int(spec.get("blacklist_status") or 200))
    latest, latest_dev = spec.get("latest"), spec.get("latest_dev")
    add("config", xcconfig(latest, latest_dev) if latest else None)
    add("dev-config", xcconfig(latest_dev or latest, latest_dev or latest) if (latest_dev or latest) else None)
    for i, r in enumerate(spec.get("routes") or []):
        r = dict(r)
        if "body_text" in r:                       # inline text from the CLI, stored as a file like the rest
            rel = f"bodies/route-{i}"
            files[rel] = r.pop("body_text")
            r["body_file"] = rel
        routes.append(r)
    cfg = {
        "info": info,
        "plists": {BUILD_DETAILS: details} if details else {},
        "routes": routes,
        "reset_defaults": CACHE_KEYS,
        "reset_token": token or uuid.uuid4().hex,
        "reset_every_launch": bool(spec.get("reset_every_launch")),
    }
    return cfg, files


def describe(spec: dict) -> str:
    parts = []
    for field in ("version", "build_number", "branch", "sha"):
        if spec.get(field) is not None:
            parts.append(f"{field}={spec[field] or '(none)'}")
    for k, v in (spec.get("build_details") or {}).items():
        parts.append(f"{k}={v}")
    if spec.get("blacklist") is not None:
        try:
            n = len(json.loads(spec["blacklist"]).get("blacklistedVersions", []))
            parts.append(f"blacklist: {n} entries")
        except (ValueError, AttributeError):
            parts.append("blacklist: (not JSON)")
    if spec.get("blacklist_status") and int(spec["blacklist_status"]) != 200:
        parts.append(f"blacklist HTTP {spec['blacklist_status']}")
    if spec.get("latest"):
        parts.append(f"latest={spec['latest']}")
    if spec.get("latest_dev"):
        parts.append(f"latest_dev={spec['latest_dev']}")
    if spec.get("fail"):
        parts.append(f"offline: {', '.join(spec['fail'])}")
    if spec.get("routes"):
        parts.append(f"{len(spec['routes'])} more route(s)")
    return ", ".join(parts) or "nothing overridden"


def entries_from(value: Any, base: Path | None = None) -> str:
    """A blacklist file's text from a scenario's `blacklist`: a list of entries, a whole {"blacklistedVersions": ...}
    object, a path (relative to `base`), or raw text (a malformed file is something to test too)."""
    if isinstance(value, list):
        return blacklist_json(value)
    if isinstance(value, dict):
        return json.dumps(value, indent=2) + "\n"
    if isinstance(value, str):
        p = Path(value).expanduser()
        if not p.is_absolute() and base is not None:
            p = base / p
        if p.is_file():
            return p.read_text()
        if value.lstrip().startswith(("{", "[")) or not value.strip():
            return value
        raise HarnessError(f"versionmock: blacklist {value!r} is neither JSON nor a file")
    raise HarnessError(f"versionmock: blacklist must be a list, an object, a path or text, not {type(value).__name__}")


# ---------------- state -----------------

def current() -> dict | None:
    return read_json(STATE_FILE)


def write_active(spec: dict, *, bundle_id: str, target: str, token: str | None = None) -> dict:
    """Writes the config into ACTIVE_DIR and records it as the mock in force for `bundle_id`. Reusing the previous
    `token` keeps the app's cached version-check answer; a new one (the default) has the shim drop it."""
    cfg, files = build_config(spec, token)
    shutil.rmtree(ACTIVE_DIR, ignore_errors=True)
    (ACTIVE_DIR / "bodies").mkdir(parents=True, exist_ok=True)
    write_json(ACTIVE_DIR / "config.json", cfg)
    for rel, text in files.items():
        (ACTIVE_DIR / rel).write_text(text)
    state = {"spec": spec, "bundle_id": bundle_id, "target": target, "token": cfg["reset_token"],
             "summary": describe(spec)}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(STATE_FILE, state)
    return state


def new_token() -> str:
    """A new reset token in the active config, so the next launch removes the cached answer again."""
    state = current()
    if not state:
        raise HarnessError("no version mock set (`th versionmock set ...`)")
    cfg = read_json(ACTIVE_DIR / "config.json", {})
    cfg["reset_token"] = state["token"] = uuid.uuid4().hex
    write_json(ACTIVE_DIR / "config.json", cfg)
    write_json(STATE_FILE, state)
    return state["token"]


# ---------------- the shim binary -----------------

SDKS = {"sim": ("iphonesimulator", ["arm64", "x86_64"], "-mios-simulator-version-min=15.0"),
        "rooted-vnc": ("iphoneos", ["arm64"], "-miphoneos-version-min=15.0")}


def shim_path(target: str) -> Path:
    """The shim built for `target`'s SDK, compiled on first use (and again when its source changes)."""
    if target not in SDKS:
        raise HarnessError(f"versionmock: no shim for target {target!r} (sim | rooted-vnc)")
    sdk, archs, minver = SDKS[target]
    digest = hashlib.sha256(SHIM_SOURCE.read_bytes()).hexdigest()[:12]
    out = WORK_DIR / f"{PHONE_TWEAK_NAME}-{sdk}-{digest}.dylib"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["xcrun", "--sdk", sdk, "clang", "-dynamiclib", "-fobjc-arc", "-fmodules", "-O1", minver,
           *[x for a in archs for x in ("-arch", a)], "-framework", "Foundation",
           "-install_name", f"@rpath/{PHONE_TWEAK_NAME}.dylib", "-o", str(out), str(SHIM_SOURCE)]
    try:
        run(cmd, timeout=300)
        run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(out)], timeout=60)
    except HarnessError:
        out.unlink(missing_ok=True)
        raise
    for stale in out.parent.glob(f"{PHONE_TWEAK_NAME}-{sdk}-*.dylib"):
        if stale != out:
            stale.unlink(missing_ok=True)
    return out


# ---------------- simulator -----------------

def sync_simulator(drv, bundle_id: str, *, fresh: bool = False) -> Path | None:
    """Copies the active config into the app's data container (Library/th-versionmock). `fresh` empties the folder
    first (a new `set`: the old log and merged resources go). None when the app is not installed."""
    if not drv.is_installed(bundle_id):
        return None
    dest = drv.app_data_container(bundle_id) / CONTAINER_SUBDIR
    if fresh:
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ACTIVE_DIR / "config.json", dest / "config.json")
    if (ACTIVE_DIR / "bodies").is_dir():
        shutil.copytree(ACTIVE_DIR / "bodies", dest / "bodies", dirs_exist_ok=True)
    return dest


def simulator_launch_env(drv, bundle_id: str) -> dict[str, str]:
    """For SimulatorDriver.launch: DYLD_INSERT_LIBRARIES for the app the mock is set for (its config synced first);
    nothing for any other app or when no mock is set."""
    state = current()
    if not state or state.get("bundle_id") != bundle_id or state.get("target") != "sim":
        return {}
    if not (ACTIVE_DIR / "config.json").exists():
        log("versionmock: state without a config; `th versionmock set` again (launching without the shim)")
        return {}
    sync_simulator(drv, bundle_id)
    return {"DYLD_INSERT_LIBRARIES": str(shim_path("sim"))}


def clear_simulator(drv, bundle_id: str) -> None:
    if drv.is_installed(bundle_id):
        shutil.rmtree(drv.app_data_container(bundle_id) / CONTAINER_SUBDIR, ignore_errors=True)


def simulator_log(drv, bundle_id: str) -> list[dict]:
    if not drv.is_installed(bundle_id):
        return []
    path = drv.app_data_container(bundle_id) / CONTAINER_SUBDIR / "log.jsonl"
    return parse_log(path.read_text() if path.exists() else "")


# ---------------- jailbroken phone -----------------

def tweak_filter_plist(bundle_id: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict><key>Filter</key><dict><key>Bundles</key><array>'
            f'<string>{bundle_id}</string></array></dict></dict></plist>\n')


def _phone_sudo(cmd: str) -> None:
    from . import devices as D
    for attempt in (f"sudo -n sh -c {json.dumps(cmd)}", cmd):
        cp = D.ssh_run(attempt, check=False, timeout=60)
        if cp.returncode == 0:
            return
    raise HarnessError(f"on the phone: {cmd!r} failed: {(cp.stderr or cp.stdout)[-300:]}")


def install_phone_shim(bundle_id: str) -> None:
    """The shim as a MobileSubstrate/ElleKit library that loads only into `bundle_id`."""
    from . import devices as D
    dylib = shim_path("rooted-vnc")
    filt = WORK_DIR / f"{PHONE_TWEAK_NAME}.plist"
    filt.write_text(tweak_filter_plist(bundle_id))
    D.ssh_push([dylib, filt], "/var/mobile/Library/Caches/")
    _phone_sudo(f"mkdir -p {PHONE_TWEAK_DIR} && "
                f"mv /var/mobile/Library/Caches/{dylib.name} {PHONE_TWEAK_DIR}/{PHONE_TWEAK_NAME}.dylib && "
                f"mv /var/mobile/Library/Caches/{filt.name} {PHONE_TWEAK_DIR}/{PHONE_TWEAK_NAME}.plist && "
                f"chmod 644 {PHONE_TWEAK_DIR}/{PHONE_TWEAK_NAME}.dylib {PHONE_TWEAK_DIR}/{PHONE_TWEAK_NAME}.plist")


def remove_phone_shim() -> None:
    _phone_sudo(f"rm -f {PHONE_TWEAK_DIR}/{PHONE_TWEAK_NAME}.dylib {PHONE_TWEAK_DIR}/{PHONE_TWEAK_NAME}.plist")


def sync_phone(bundle_id: str, *, fresh: bool = False) -> str:
    from . import devices as D
    dest = f"{D.ssh_app_container(bundle_id)}/{CONTAINER_SUBDIR}"
    D.ssh_run(f"{'rm -rf ' + repr(dest) + ' && ' if fresh else ''}mkdir -p '{dest}/bodies'", timeout=30)
    D.ssh_push([ACTIVE_DIR / "config.json"], dest + "/")
    bodies = sorted((ACTIVE_DIR / "bodies").glob("*"))
    if bodies:
        D.ssh_push(bodies, dest + "/bodies/")
    return dest


def clear_phone(bundle_id: str) -> None:
    from . import devices as D
    D.ssh_run(f"rm -rf '{D.ssh_app_container(bundle_id)}/{CONTAINER_SUBDIR}'", check=False, timeout=30)


def phone_log(bundle_id: str) -> list[dict]:
    from . import devices as D
    cp = D.ssh_run(f"cat '{D.ssh_app_container(bundle_id)}/{CONTAINER_SUBDIR}/log.jsonl' 2>/dev/null", check=False,
                   timeout=30)
    return parse_log(cp.stdout)


# ---------------- both -----------------

def parse_log(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def activate(spec: dict, *, target: str, bundle_id: str, drv=None, install_shim: bool = True,
             token: str | None = None) -> dict:
    """Sets the mock for the app's next launch on `target` (sim or rooted-vnc). `install_shim=False` skips putting
    the shim on the phone again (a scenario run does it once)."""
    if target not in SDKS:
        raise HarnessError(f"versionmock works on the simulator and the jailbroken phone (--target sim | rooted-vnc), "
                           f"not {target}")
    try:
        shim_path(target)                    # fail now, not at launch, when it does not build
    except HarnessError as e:
        if target != "sim":
            raise
        # a job's agent may not run the compiler in its sandbox; the daemon, which launches the app, builds it then
        log(f"versionmock: the shim did not build here ({str(e).splitlines()[0][:200]}); the launch builds it")
    state = write_active(spec, bundle_id=bundle_id, target=target, token=token)
    if target == "sim":
        if sync_simulator(drv, bundle_id, fresh=True) is None:
            log(f"versionmock: {bundle_id} is not installed yet; the config goes into its container at launch")
    else:
        if install_shim:
            install_phone_shim(bundle_id)
        sync_phone(bundle_id, fresh=True)
    return state


def deactivate(*, target: str, drv=None, remove_shim: bool = False) -> dict | None:
    state = current()
    bundle_id = (state or {}).get("bundle_id")
    if bundle_id:
        if target == "sim":
            clear_simulator(drv, bundle_id)
        elif target == "rooted-vnc":
            clear_phone(bundle_id)
    if target == "rooted-vnc" and remove_shim:
        remove_phone_shim()
    STATE_FILE.unlink(missing_ok=True)
    shutil.rmtree(ACTIVE_DIR, ignore_errors=True)
    return state


def resync(*, target: str, drv=None) -> None:
    """After new_token(): the changed config into the container now (the simulator also does it at every launch)."""
    state = current()
    if not state:
        return
    if target == "sim":
        sync_simulator(drv, state["bundle_id"])
    elif target == "rooted-vnc":
        sync_phone(state["bundle_id"])


def served(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("event") == "request"]


# ---------------- reading the result off the screen -----------------

def read_alert(drv, shot: Path | None = None) -> dict | None:
    """The version alert on screen: {"kind", "title", "message"} or None. Accessibility tree where there is one,
    else OCR of `shot` (the message is then OCR text)."""
    nodes = drv.ui_tree()
    if nodes:
        for title, kind in ALERT_TITLES.items():
            if any(n.matches(title, exact=True) for n in nodes):
                anchor = BLACKLIST_TEXT[:40] if kind == "update-required" else AVAILABLE_TEXT[:25]
                msg = next((n.label or n.value for n in nodes if n.matches(anchor)), "")
                return {"kind": kind, "title": title, "message": msg}
        return None
    if shot is None:
        return None
    from .ocr import dump_text
    text = " ".join(dump_text(shot).split())
    for title, kind in ALERT_TITLES.items():
        if title.lower() in text.lower():
            return {"kind": kind, "title": title, "message": text, "ocr": True}
    return None


def norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def message_matches(alert: dict, expected: str | None) -> bool:
    """Whether the alert carries the expected entry message (`None`: only the standard text, nothing after it)."""
    msg = norm(alert.get("message", ""))
    if expected is None:
        if alert.get("ocr"):
            return True          # OCR text also holds whatever is behind the alert: no way to tell
        return msg.endswith(norm(BLACKLIST_TEXT)) or msg == "" or alert["kind"] != "update-required"
    return norm(expected) in msg


def verdict(expect: dict, alert: dict | None) -> tuple[bool, str]:
    """Compares what a scenario expects ({"alert": kind, "message": text|None}) with the alert read off the screen."""
    want = expect.get("alert", "none")
    got = alert["kind"] if alert else "none"
    if want != got:
        return False, f"expected {want}, saw {got}" + (f" ({alert.get('message', '')[:120]!r})" if alert else "")
    if got == "update-required" and "message" in expect and not message_matches(alert, expect["message"]):
        exp = expect["message"]
        return False, (f"alert without the entry message {exp!r}" if exp else "alert has an entry message, expected "
                       "only the standard text") + f": {alert.get('message', '')[:200]!r}"
    return True, f"{got}" + (f": {alert.get('message', '')[:200]!r}" if alert and alert.get("message") else "")


# ---------------- scenario files -----------------

def load_scenarios(path: Path) -> tuple[dict, list[dict]]:
    """A scenario file: {"defaults": {...spec...}, "scenarios": [{"name", ...spec..., "expect": {...}}]}.
    Spec keys as in build_config; `blacklist` as in entries_from (relative paths from the file's folder)."""
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("scenarios"), list) or not data["scenarios"]:
        raise HarnessError(f"{path}: needs a non-empty \"scenarios\" list")
    defaults = data.get("defaults") or {}
    out = []
    for i, sc in enumerate(data["scenarios"]):
        merged = {**defaults, **sc}
        merged.setdefault("name", f"scenario-{i + 1}")
        if merged.get("blacklist") is not None:
            merged["blacklist"] = entries_from(merged["blacklist"], path.parent)
        for key in ("expect", "expect_legacy"):
            exp = merged.get(key) or {}
            if not isinstance(exp, dict) or exp.get("alert", "none") not in ALERT_KINDS:
                raise HarnessError(f"{path}: {merged['name']}: {key}.alert is one of {', '.join(ALERT_KINDS)}")
        if merged.get("latest") is None and merged.get("version") and "latest" not in sc and "latest" not in defaults:
            merged["latest"] = merged["version"]     # deterministic: no "Update Available" unless asked for
        out.append(merged)
    return data, out


def part(scenarios: list[dict], spec: str) -> list[dict]:
    """The I-th of N consecutive parts ("I/N", 1-based) of the scenarios, of near-equal length; a scenario with
    "reset": false stays in the same part as the one before it (it tests that one's cache)."""
    try:
        i, n = (int(x) for x in spec.split("/"))
    except ValueError:
        raise HarnessError(f"--part {spec!r}: I/N, e.g. 2/4") from None
    if not 1 <= i <= n:
        raise HarnessError(f"--part {spec!r}: I from 1 to N")
    units: list[list[dict]] = []
    for sc in scenarios:
        if sc.get("reset") is False and units:
            units[-1].append(sc)
        else:
            units.append([sc])
    total = len(scenarios)
    parts: list[list[dict]] = [[] for _ in range(n)]
    done = 0
    for u in units:
        k = min(n - 1, done * n // total) if total else 0
        parts[k].extend(u)
        done += len(u)
    return parts[i - 1]


SPEC_KEYS = ("version", "build_number", "branch", "sha", "build_details", "info", "blacklist", "blacklist_status",
             "latest", "latest_dev", "fail", "routes", "reset_every_launch")


def spec_of(scenario: dict) -> dict:
    return {k: scenario[k] for k in SPEC_KEYS if k in scenario}


# ---------------- driving the app -----------------

def relaunch(drv, target: str, bundle_id: str, app_name: str) -> None:
    """Terminate and launch the app so the shim (re)reads its config."""
    if target == "sim":
        drv.launch(bundle_id, terminate_first=True)
        return
    from . import devices as D
    import time
    D.ssh_kill(app_name)
    time.sleep(1.5)
    D.ssh_launch(bundle_id)


def events(drv, target: str, bundle_id: str) -> list[dict]:
    return simulator_log(drv, bundle_id) if target == "sim" else phone_log(bundle_id)


def wait_for_result(drv, target: str, bundle_id: str, *, timeout: float = 20.0, grace: float = 4.0,
                    shot: Path | None = None) -> tuple[dict | None, list[dict]]:
    """Waits for the version alert, or, when none comes, until the blacklist was served and `grace` seconds more
    passed (or `timeout`). Returns (alert or None, the requests the shim answered)."""
    import time
    from .util import tmp_path
    shot = shot or tmp_path("versionmock-check.png")
    deadline = time.monotonic() + timeout
    served_at = None
    while True:
        drv.screenshot(shot)
        alert = read_alert(drv, shot)
        reqs = served(events(drv, target, bundle_id))
        if alert:
            return alert, reqs
        now = time.monotonic()
        if served_at is None and any(r.get("url") == URLS["blacklist"] for r in reqs):
            served_at = now
        if (served_at is not None and now - served_at >= grace) or now >= deadline:
            return None, reqs
        time.sleep(1.0)
