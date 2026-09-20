"""Configuration: harness.env (repo root) + environment variables + defaults.

Every value can be overridden by an environment variable of the same name.
Never put secrets in the repo; harness.env is gitignored.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / "harness.env"

DEFAULTS = {
    "TRIO_REPO": str(REPO_ROOT / "Trio"),        # Trio checkout the harness builds from (cloned on demand)
    "TH_TRIO_REFERENCE": str(REPO_ROOT.parent / "Trio"),  # existing local clone used to speed up that clone
    "TH_TRIO_BASE_BRANCH": "dev",                # Trio branch to test submodule PRs against when not derivable
    "TH_DEVELOPER_TEAM": "",          # Apple Team ID for device builds; blank = simulator-only
    "TH_SIM_TEAM_PLACEHOLDER": "TRIOSIMTST",  # fake team id used to form a valid bundle id on the simulator
    "TH_XCODE_APP": "",               # e.g. /Applications/Xcode-26.3.app; blank = auto-detect newest
    "TH_SIM_DEVICE": "iPhone 16",     # simulator device type name
    "TH_SIM_RUNTIME": "",             # e.g. "iOS 26.3"; blank = newest installed iOS runtime
    "TH_SIM_NAME": "TrioHarness",     # name of the dedicated simulator we create/boot
    "TH_DEVICE_UDID": "",             # physical iPhone UDID; blank = the only connected device
    "TH_VNC_HOST": "localhost",       # TrollVNC host. localhost + TH_VNC_VIA_USB=1 => iproxy over USB
    "TH_VNC_PORT": "5901",
    "TH_VNC_LOCAL_PORT": "5901",      # local port iproxy listens on
    "TH_VNC_PASSWORD": "",
    "TH_VNC_VIA_USB": "1",
    "TH_DEVICE_PASSCODE": "",         # phone passcode for `th device unlock` (put it in .env only; never commit)
    "TH_SSH_HOST": "",                # jailbroken phone SSH host; blank = TH_VNC_HOST
    "TH_SSH_USER": "mobile",
    "TH_SSH_PORT": "22",
    "TH_IPA_HTTP_PORT": "8765",       # local HTTP server used for TrollStore installs
    "TH_IPA_HTTP_HOST": "",           # IP/hostname the iPhone can reach; blank = auto (LAN IP)
    "TH_ARTIFACT_REPO": "jwoglom/trio-test-screenshots",  # GitHub repo that hosts screenshots for PR comments
    "TH_RUNS_DIR": str(REPO_ROOT / "runs"),
    "TH_DERIVED_DATA": str(Path.home() / "Library/Developer/Xcode/DerivedData/trio-test-harness"),
    "TH_BUILD_DIR": str(REPO_ROOT / "build"),
    "TH_TRIO_SCHEME": "Trio",
    "TH_TRIO_WORKSPACE": "Trio.xcworkspace",
}


def _load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0].split("\t#", 1)[0].strip().strip('"').strip("'")
        out[k.strip()] = os.path.expanduser(v)
    return out


@dataclass
class Config:
    values: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        vals = dict(DEFAULTS)
        vals.update(_load_env_file(ENV_FILE))
        vals.update(_load_env_file(REPO_ROOT / ".env"))   # optional overrides (also gitignored)
        for k in DEFAULTS:
            if k in os.environ:
                vals[k] = os.environ[k]
        return cls(vals)

    def __getattr__(self, item: str) -> str:
        key = item.upper()
        if key in self.values:
            return self.values[key]
        if "TH_" + key in self.values:
            return self.values["TH_" + key]
        raise AttributeError(item)

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def bool(self, key: str) -> bool:
        return self.values.get(key, "").lower() in ("1", "true", "yes", "on")

    @property
    def trio_repo(self) -> Path:
        return Path(self.values["TRIO_REPO"]).expanduser()

    @property
    def runs_dir(self) -> Path:
        p = Path(self.values["TH_RUNS_DIR"]).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def build_dir(self) -> Path:
        p = Path(self.values["TH_BUILD_DIR"]).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def team_for(self, target: str) -> str:
        """Team ID used in ConfigOverride.xcconfig for a build target ('sim' or 'device')."""
        if target == "device":
            return self.values["TH_DEVELOPER_TEAM"]
        return self.values["TH_DEVELOPER_TEAM"] or self.values["TH_SIM_TEAM_PLACEHOLDER"]

    def bundle_id(self, target: str) -> str:
        return f"org.nightscout.{self.team_for(target)}.trio"


CONFIG = Config.load()
