"""Check out a Trio branch / PR (optionally with submodule overrides), and build it
for the iOS Simulator or a physical iPhone.

Simulator builds need no Apple credentials (ad-hoc signing, same as Trio CI).
Device builds either:
  * sign automatically with TH_DEVELOPER_TEAM (requires an Apple ID in Xcode or an
    App Store Connect API key), then install with devicectl; or
  * are ad-hoc signed and packaged as an .ipa for TrollStore (no Apple credentials).
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import zipfile
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .config import CONFIG, REPO_ROOT
from .util import HarnessError, log, run, write_json, read_json
from . import xcode

DEFAULT_GH_REPO = "nightscout/Trio"
SRC_DIR = REPO_ROOT / "build" / "Trio-src"
LAST_BUILD = REPO_ROOT / "build" / "last-build.json"


@dataclass
class SubmoduleOverride:
    name: str
    ref: str                 # branch name, "pr/123", or a commit sha
    url: str | None = None   # alternate remote (fork) URL

    @classmethod
    def parse(cls, spec: str) -> "SubmoduleOverride":
        # forms: NAME@REF   NAME=URL#REF   NAME@pr/123
        m = re.match(r"^([\w.-]+)=(.+?)#(.+)$", spec)
        if m:
            return cls(m.group(1), m.group(3), m.group(2))
        m = re.match(r"^([\w.-]+)@(.+)$", spec)
        if m:
            return cls(m.group(1), m.group(2))
        raise HarnessError(f"bad --submodule spec {spec!r}; use NAME@BRANCH, NAME@pr/123 or NAME=URL#BRANCH")


@dataclass
class BuildInfo:
    target: str                     # sim | device | device-trollstore
    configuration: str
    source_dir: str
    app_path: str
    bundle_id: str
    ipa_path: str | None
    git: dict = field(default_factory=dict)
    xcode: str = ""
    scheme: str = ""

    def save(self) -> None:
        LAST_BUILD.parent.mkdir(parents=True, exist_ok=True)
        write_json(LAST_BUILD, asdict(self))

    @classmethod
    def load(cls) -> "BuildInfo | None":
        d = read_json(LAST_BUILD)
        return cls(**d) if d else None


# ---------------- source checkout -----------------

# Process-scoped git config: use SSH for GitHub (private submodules such as TandemKit need auth,
# and the user's origin already uses SSH), never prompt for credentials.
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "url.git@github.com:.insteadOf",
    "GIT_CONFIG_VALUE_0": "https://github.com/",
}


def _git(args: list[str], cwd: Path, **kw):
    env = {**GIT_ENV, **(kw.pop("env", None) or {})}
    return run(["git", *args], cwd=cwd, env=env, **kw)


def _check_submodules(src: Path) -> None:
    bad = [l.split()[1] for l in _git(["submodule", "status", "--recursive"], src, quiet=True).stdout.splitlines()
           if l.startswith("-")]
    if bad:
        raise HarnessError(f"submodules not initialized: {bad} (clone failed? check SSH access to their repos)")


def _origin_url(repo: Path) -> str:
    return _git(["remote", "get-url", "origin"], repo, quiet=True).stdout.strip()


def _gh_repo_url(slug: str) -> str:
    return f"https://github.com/{slug}.git"


def resolve_pr(pr: str) -> tuple[str, int]:
    """'123' | 'owner/repo#123' | GitHub PR URL -> (owner/repo, number)."""
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", pr)
    if m:
        return m.group(1), int(m.group(2))
    m = re.match(r"^([^/#]+/[^/#]+)#(\d+)$", pr)
    if m:
        return m.group(1), int(m.group(2))
    if pr.isdigit():
        return CONFIG.get("TH_DEFAULT_GH_REPO", DEFAULT_GH_REPO), int(pr)
    raise HarnessError(f"cannot parse PR reference {pr!r}")


def prepare_source(*, branch: str | None = None, pr: str | None = None, repo: str | None = None,
                   in_place: Path | None = None, submodules: list[str] | None = None,
                   fetch: bool = True) -> tuple[Path, dict]:
    """Return (source_dir, git_info)."""
    info: dict = {}
    if in_place or (not branch and not pr):
        src = Path(in_place or CONFIG.trio_repo).expanduser().resolve()
        if not (src / "Trio.xcworkspace").exists():
            raise HarnessError(f"{src} does not look like a Trio checkout")
        log(f"building in place from {src} (current checkout, uncommitted changes included)")
    else:
        src = SRC_DIR
        base_url = _gh_repo_url(repo) if repo and "/" in repo else (repo or None)
        if not src.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            local = CONFIG.trio_repo
            if local.exists():
                # local clone shares objects (fast), then point origin at the real remote
                _git(["clone", "--no-checkout", str(local), str(src)], REPO_ROOT)
                _git(["remote", "set-url", "origin", base_url or _origin_url(local)], src)
            else:
                _git(["clone", "--no-checkout", base_url or _gh_repo_url(DEFAULT_GH_REPO), str(src)], REPO_ROOT)
        elif base_url:
            _git(["remote", "set-url", "origin", base_url], src)
        # make sure the canonical upstream is available too
        remotes = _git(["remote"], src, quiet=True).stdout.split()
        if "upstream" not in remotes:
            _git(["remote", "add", "upstream", _gh_repo_url(DEFAULT_GH_REPO)], src, check=False)
        if pr:
            slug, num = resolve_pr(pr)
            remote_url = _gh_repo_url(slug)
            _git(["fetch", "--force", remote_url, f"pull/{num}/head:refs/th/pr-{num}"], src, timeout=900)
            _git(["checkout", "--force", "--detach", f"refs/th/pr-{num}"], src)
            info["pr"] = f"{slug}#{num}"
            try:
                meta = json.loads(run(["gh", "pr", "view", str(num), "--repo", slug, "--json",
                                       "title,headRefName,headRepositoryOwner,url"], quiet=True).stdout)
                info["pr_title"] = meta.get("title")
                info["branch"] = f"{meta.get('headRepositoryOwner', {}).get('login')}:{meta.get('headRefName')}"
                info["pr_url"] = meta.get("url")
            except HarnessError:
                pass
        else:
            assert branch
            # branch may be 'owner/repo@branch' or 'URL#branch' or plain 'branch'
            m = re.match(r"^(.+?)#(.+)$", branch) or re.match(r"^([\w.-]+/[\w.-]+)@(.+)$", branch)
            if m:
                url = m.group(1) if "://" in m.group(1) or m.group(1).startswith("git@") else _gh_repo_url(m.group(1))
                br = m.group(2)
            else:
                url, br = "origin", branch
            _git(["fetch", "--force", url, f"{br}:refs/th/branch"], src, timeout=900)
            _git(["checkout", "--force", "--detach", "refs/th/branch"], src)
            info["branch"] = branch
        _git(["clean", "-fdx", "-e", "ConfigOverride.xcconfig"], src, quiet=True)
    if fetch:
        log("syncing git submodules (first run downloads LoopKit & friends; several minutes)")
        _git(["submodule", "sync", "--recursive"], src, quiet=True)
        _git(["submodule", "update", "--init", "--recursive", "--jobs", "6"], src, timeout=3600)
        _check_submodules(src)
    for spec in submodules or []:
        ov = SubmoduleOverride.parse(spec)
        sub = src / ov.name
        if not (sub / ".git").exists() and not (sub / ".git").is_file():
            raise HarnessError(f"submodule {ov.name} not found under {src}")
        ref = ov.ref
        if ref.startswith("pr/"):
            ref = f"pull/{ref[3:]}/head"
        remote = ov.url or "origin"
        _git(["fetch", "--force", remote, ref], sub, timeout=900)
        _git(["checkout", "--force", "--detach", "FETCH_HEAD"], sub)
        _git(["submodule", "update", "--init", "--recursive"], sub, check=False, timeout=1800)
        info.setdefault("submodule_overrides", []).append(asdict(ov))
    info["sha"] = _git(["rev-parse", "--short=10", "HEAD"], src, quiet=True).stdout.strip()
    info["describe"] = _git(["log", "-1", "--format=%s"], src, quiet=True).stdout.strip()
    info["submodules"] = {}
    for line in _git(["submodule", "status", "--recursive"], src, quiet=True, check=False).stdout.splitlines():
        parts = line.strip().lstrip("+-U").split()
        if len(parts) >= 2:
            info["submodules"][parts[1]] = parts[0][:10]
    return src, info


# ---------------- ConfigOverride.xcconfig -----------------

def write_config_override(src: Path, target: str, *, dev_warning: bool = False) -> str:
    """Ensure ConfigOverride.xcconfig exists with a usable team. Returns the team used."""
    path = src / "ConfigOverride.xcconfig"
    team = CONFIG.team_for(target)
    if target == "device" and not team:
        raise HarnessError("device builds need TH_DEVELOPER_TEAM in harness.env (or use --trollstore)")
    lines = [
        "// generated by trio-test-harness; safe to delete",
        f"DEVELOPER_TEAM = {team}",
        f"BUNDLE_IDENTIFIER = org.nightscout.{team}.trio",
        f"TRIO_APP_GROUP_ID = group.org.nightscout.{team}.trio.trio-app-group",
    ]
    if not dev_warning:
        lines.append("SWIFT_ACTIVE_COMPILATION_CONDITIONS = $(inherited) DEV_BRANCH_WARNING_DISABLED")
    existing = path.read_text() if path.exists() else ""
    if existing and "generated by trio-test-harness" not in existing:
        log(f"keeping your existing {path} (not generated by the harness)")
        m = re.search(r"DEVELOPER_TEAM\s*=\s*(\S+)", existing)
        return m.group(1) if m else team
    path.write_text("\n".join(lines) + "\n")
    return team


def _bundle_id_from_app(app: Path) -> str:
    info = plistlib.loads((app / "Info.plist").read_bytes())
    return info["CFBundleIdentifier"]


# ---------------- xcodebuild -----------------

COMMON_FLAGS = [
    "-onlyUsePackageVersionsFromResolvedFile", "-disableAutomaticPackageResolution",
    "-skipPackagePluginValidation", "-skipMacroValidation",
    "COMPILER_INDEX_STORE_ENABLE=NO",
]
ADHOC_SIGNING = ['CODE_SIGN_IDENTITY=-', "CODE_SIGN_STYLE=Manual", "DEVELOPMENT_TEAM=", "PROVISIONING_PROFILE_SPECIFIER="]


def resolve_packages(src: Path, scheme: str) -> None:
    run(["xcodebuild", "-resolvePackageDependencies", "-workspace", CONFIG.get("TH_TRIO_WORKSPACE"),
         "-scheme", scheme, "-onlyUsePackageVersionsFromResolvedFile", "-derivedDataPath", CONFIG.get("TH_DERIVED_DATA")],
        cwd=src, env={**xcode.developer_env(), "CI": "true"}, timeout=1800, capture=False)


def build(src: Path, target: str, *, configuration: str = "Debug", clean: bool = False,
          git_info: dict | None = None, log_path: Path | None = None, trollstore: bool = False) -> BuildInfo:
    scheme = CONFIG.get("TH_TRIO_SCHEME")
    workspace = CONFIG.get("TH_TRIO_WORKSPACE")
    dd = Path(CONFIG.get("TH_DERIVED_DATA")).expanduser()
    env = {**xcode.developer_env(), "CI": "true"}  # CI=true skips SwiftFormat + Crashlytics phases
    if clean and dd.exists():
        shutil.rmtree(dd)
    team = write_config_override(src, "device" if (target == "device" and not trollstore) else "sim")
    if not (dd / "SourcePackages").exists():
        resolve_packages(src, scheme)
    if target == "sim":
        dest = "generic/platform=iOS Simulator"
        signing = ADHOC_SIGNING
        products = f"{configuration}-iphonesimulator"
    else:
        dest = "generic/platform=iOS"
        products = f"{configuration}-iphoneos"
        if trollstore:
            # Xcode 26 refuses CODE_SIGN_IDENTITY=- for the iphoneos SDK ("Ad Hoc code signing is not allowed"),
            # so build unsigned and fake-sign afterwards with codesign (AppSync / TrollStore accept that).
            signing = ["CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO", "CODE_SIGN_IDENTITY=",
                       "DEVELOPMENT_TEAM=", "PROVISIONING_PROFILE_SPECIFIER="]
        else:
            signing = ["CODE_SIGN_STYLE=Automatic", f"DEVELOPMENT_TEAM={team}",
                       "-allowProvisioningUpdates", "-allowProvisioningDeviceRegistration"]
    cmd = ["xcodebuild", "build", "-workspace", workspace, "-scheme", scheme, "-configuration", configuration,
           "-destination", dest, "-derivedDataPath", str(dd), *COMMON_FLAGS, *signing]
    log(f"xcodebuild ({target}, {configuration}) -> {dd}")
    log_path = log_path or (REPO_ROOT / "build" / f"xcodebuild-{target}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as fh:
        import subprocess
        p = subprocess.Popen(cmd, cwd=src, env={**os.environ, **env}, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        tail: list[str] = []
        for line in p.stdout:  # type: ignore[union-attr]
            fh.write(line)
            tail.append(line)
            if len(tail) > 60:
                tail.pop(0)
            if re.search(r"error:|BUILD SUCCEEDED|BUILD FAILED|\*\* ", line):
                print(line.rstrip(), flush=True)
        p.wait()
    if p.returncode != 0:
        errs = [l for l in Path(log_path).read_text().splitlines() if "error:" in l]
        raise HarnessError("xcodebuild failed. Errors:\n" + "\n".join(errs[-20:]) + f"\nfull log: {log_path}")
    app = dd / "Build" / "Products" / products / "Trio.app"
    if not app.exists():
        raise HarnessError(f"build succeeded but {app} is missing")
    ipa = None
    if target == "device" and trollstore:
        fakesign_app(app, src, team, _bundle_id_from_app(app))
        ipa = package_ipa(app, CONFIG.build_dir / "Trio.ipa")
    bi = BuildInfo(target="device-trollstore" if trollstore else target, configuration=configuration,
                   source_dir=str(src), app_path=str(app), bundle_id=_bundle_id_from_app(app),
                   ipa_path=str(ipa) if ipa else None, git=git_info or {},
                   xcode=xcode.xcode_version(xcode.find_xcode()), scheme=scheme)
    bi.save()
    log(f"built {bi.bundle_id} at {app}")
    return bi


def _entitlements_for(bundle: Path, src: Path, team: str, app_group: str) -> dict:
    """Entitlements for a bundle: Trio's own file (with $(APP_GROUP_ID) resolved) for the main app, plus the
    identifier entitlements Xcode would normally inject. Extensions get just the identifier + app group."""
    info = plistlib.loads((bundle / "Info.plist").read_bytes())
    bid = info["CFBundleIdentifier"]
    ents: dict = {
        "application-identifier": f"{team}.{bid}",
        "com.apple.developer.team-identifier": team,
        "keychain-access-groups": [f"{team}.{bid}"],
        "get-task-allow": True,
    }
    if bundle.suffix == ".app" and bundle.name == "Trio.app":
        ents.update(plistlib.loads((src / "Trio/Resources/Trio.entitlements").read_bytes()))
    ents["com.apple.security.application-groups"] = [app_group]
    def subst(v):
        if isinstance(v, str):
            return v.replace("$(APP_GROUP_ID)", app_group).replace("$(TRIO_APP_GROUP_ID)", app_group)
        if isinstance(v, list):
            return [subst(x) for x in v]
        if isinstance(v, dict):
            return {k: subst(x) for k, x in v.items()}
        return v
    return subst(ents)


def fakesign_app(app: Path, src: Path, team: str, bundle_id: str) -> None:
    """codesign -s - (ad-hoc) every nested bundle, innermost first, then the app itself."""
    app_group = f"group.{bundle_id}.trio-app-group"
    nested: list[Path] = []
    # every nested code object, innermost first: dylibs anywhere (incl. Xcode's Trio.debug.dylib at the app root),
    # frameworks, app extensions, the watch app
    for pattern in ("**/*.dylib", "Watch/*.app/Frameworks/*.framework", "Watch/*.app/PlugIns/*.appex", "Watch/*.app",
                    "Frameworks/*.framework", "PlugIns/*.appex", "Extensions/*.appex"):
        for b in sorted(app.glob(pattern)):
            if b not in nested and not any(str(b).startswith(str(n) + "/") for n in nested if n.suffix == ".framework"):
                nested.append(b)
    log(f"fake-signing {len(nested)} nested bundles + {app.name} (team {team})")
    for b in nested + [app]:
        cmd = ["codesign", "--force", "--sign", "-", "--timestamp=none", "--generate-entitlement-der"]
        if b.suffix in (".app", ".appex"):
            ents = _entitlements_for(b, src, team, app_group)
            ent_file = b.parent / (b.name + ".entitlements.plist") if b is not app else CONFIG.build_dir / "Trio.entitlements.plist"
            ent_file.write_bytes(plistlib.dumps(ents))
            cmd += ["--entitlements", str(ent_file)]
        cmd.append(str(b))
        run(cmd, quiet=True)
        if b is not app and b.suffix in (".app", ".appex"):
            (b.parent / (b.name + ".entitlements.plist")).unlink(missing_ok=True)
    run(["codesign", "-dv", "--entitlements", "-", str(app)], check=False, quiet=True)


def package_ipa(app: Path, out: Path) -> Path:
    """Zip Trio.app into Payload/ -> .ipa (what TrollStore expects)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in app.rglob("*"):
            arc = Path("Payload") / app.name / p.relative_to(app)
            if p.is_symlink():
                zi = zipfile.ZipInfo(str(arc))
                zi.create_system = 3
                zi.external_attr = (0o120755 << 16)
                zf.writestr(zi, os.readlink(p))
            elif p.is_file():
                zf.write(p, str(arc))
    log(f"packaged {out} ({out.stat().st_size // (1024*1024)} MB)")
    return out
