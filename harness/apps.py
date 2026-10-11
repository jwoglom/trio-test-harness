"""The two apps the harness builds and drives: Trio and Loop.

`--app` on `th` / `trio-test-e2e` (or TH_APP) picks one; Trio is the default. Everything that differs between them
lives here, so checkout, build, install, launch and the UI flows take the app from `current()`:

            Trio                                   Loop
  source    nightscout/Trio, Trio.xcworkspace      LoopKit/LoopWorkspace, LoopWorkspace.xcworkspace
  scheme    Trio                                   LoopWorkspace (Loop, its extensions and every .loopplugin)
  bundle    org.nightscout.<team>.trio             com.<team>.loopkit.Loop: Loop.xcconfig's MAIN_APP_BUNDLE_IDENTIFIER
                                                   plus the Loop project's $(inherited).Loop
  group     group.<bundle>.trio-app-group          group.<bundle>Group (its store, settings and onboarding live there)
  flows     harness/flows/trio.py                  harness/flows/loop.py
  docs      https://triodocs.org                   https://loopkit.github.io/loopdocs

Each app has its own checkout and DerivedData (TRIO_REPO / LOOP_REPO, TH_DERIVED_DATA / TH_LOOP_DERIVED_DATA): the two
workspaces build products of the same names (LoopKit.framework from different forks) that would invalidate each other.
"""
from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG, REPO_ROOT
from .util import HarnessError


@dataclass(frozen=True)
class App:
    key: str
    name: str                       # product, process and display name
    default_repo: str               # where the checkout comes from; a bare PR number is a PR here
    repo_key: str                   # config keys: the checkout, a local clone to borrow objects from, its origin,
    reference_key: str              # the branch a cherry-pick or an empty spec starts from, and the DerivedData
    origin_key: str
    base_branch_key: str
    derived_data_key: str
    workspace_key: str
    scheme_key: str
    submodule_branches: tuple[str, ...]   # where a submodule PR's pin is looked for (after --trio-branch, the base)
    entitlements: str               # the main app's entitlements in the checkout, for fake-signing
    team_files: tuple[tuple[str, str], ...]   # (path from the checkout, setting) a user's own team is read from
    docs_url: str
    flows_module: str

    @property
    def checkout(self) -> Path:
        return Path(CONFIG.get(self.repo_key)).expanduser().resolve()

    @property
    def workspace(self) -> str:
        return CONFIG.get(self.workspace_key)

    @property
    def scheme(self) -> str:
        return CONFIG.get(self.scheme_key)

    @property
    def derived_data(self) -> Path:
        return Path(CONFIG.get(self.derived_data_key)).expanduser()

    @property
    def base_branch(self) -> str:
        return CONFIG.get(self.base_branch_key) or "dev"

    @property
    def repo_name(self) -> str:
        return self.default_repo.rsplit("/", 1)[-1].lower()

    @property
    def flows(self):
        return importlib.import_module(self.flows_module)

    @property
    def product(self) -> str:
        return f"{self.name}.app"

    @property
    def last_build(self) -> Path:
        """What `th install` / `th launch` / --skip-build use: the app's own last build."""
        return REPO_ROOT / "build" / ("last-build.json" if self.key == "trio" else f"last-build-{self.key}.json")

    def bundle_id(self, team: str) -> str:
        return f"org.nightscout.{team}.trio" if self.key == "trio" else f"com.{team}.loopkit.Loop"

    def app_group(self, bundle_id: str) -> str:
        return f"group.{bundle_id}.trio-app-group" if self.key == "trio" else f"group.{bundle_id}Group"

    def team_settings(self, team: str, *, from_user: bool = False) -> list[str]:
        """xcconfig lines that put the bundle ids and app group on `team`. A team from the user's own override file
        stays theirs for Trio; Loop derives its ids from DEVELOPMENT_TEAM, which an ad hoc build clears, so its main
        bundle id is always given outright."""
        if self.key == "trio":
            return [] if from_user else [f"DEVELOPER_TEAM = {team}", f"BUNDLE_IDENTIFIER = {self.bundle_id(team)}",
                                         f"TRIO_APP_GROUP_ID = {self.app_group(self.bundle_id(team))}"]
        return ([] if from_user else [f"LOOP_DEVELOPMENT_TEAM = {team}"]) + \
            [f"MAIN_APP_BUNDLE_IDENTIFIER = {self.bundle_id(team)}"]

    def user_team(self, src: Path) -> str | None:
        """The team from the user's own override file for this checkout, if any (read only, never modified)."""
        for rel, key in self.team_files:
            path = (src / rel).resolve()
            if not path.is_file():
                continue
            m = re.search(rf"^\s*{key}\s*=\s*([A-Za-z0-9]{{6,}})\s*$", path.read_text(errors="replace"), re.M)
            if m:
                return m.group(1)
        return None


TRIO = App(
    key="trio", name="Trio", default_repo="nightscout/Trio",
    repo_key="TRIO_REPO", reference_key="TH_TRIO_REFERENCE", origin_key="TH_TRIO_ORIGIN",
    base_branch_key="TH_TRIO_BASE_BRANCH", derived_data_key="TH_DERIVED_DATA",
    workspace_key="TH_TRIO_WORKSPACE", scheme_key="TH_TRIO_SCHEME",
    submodule_branches=("dev", "feat/tandem", "main"),
    entitlements="Trio/Resources/Trio.entitlements",
    team_files=(("ConfigOverride.xcconfig", "DEVELOPER_TEAM"),),
    docs_url="https://triodocs.org/", flows_module="harness.flows.trio",
)
LOOP = App(
    key="loop", name="Loop", default_repo="LoopKit/LoopWorkspace",
    repo_key="LOOP_REPO", reference_key="TH_LOOP_REFERENCE", origin_key="TH_LOOP_ORIGIN",
    base_branch_key="TH_LOOP_BASE_BRANCH", derived_data_key="TH_LOOP_DERIVED_DATA",
    workspace_key="TH_LOOP_WORKSPACE", scheme_key="TH_LOOP_SCHEME",
    submodule_branches=("feat/tandem-closed-beta", "dev", "main"),
    entitlements="Loop/Loop/Loop.entitlements",
    # LoopWorkspace's own LoopConfigOverride.xcconfig includes ../../LoopConfigOverride.xcconfig, where Loop's
    # build docs put a builder's team
    team_files=(("LoopConfigOverride.xcconfig", "LOOP_DEVELOPMENT_TEAM"), ("../../LoopConfigOverride.xcconfig", "LOOP_DEVELOPMENT_TEAM")),
    docs_url="https://loopkit.github.io/loopdocs/", flows_module="harness.flows.loop",
)
APPS = {"trio": TRIO, "loop": LOOP}


def get(key: str) -> App:
    try:
        return APPS[key.lower()]
    except KeyError:
        raise HarnessError(f"unknown app {key!r} ({' | '.join(APPS)})") from None


def current() -> App:
    return get(os.environ.get("TH_APP") or CONFIG.get("TH_APP") or "trio")


def select(key: str | None) -> App:
    """Make `key` the current app for this process and the harness commands it starts (the daemon, the agent)."""
    if key:
        os.environ["TH_APP"] = get(key).key
    return current()
