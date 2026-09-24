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
LAST_BUILD = REPO_ROOT / "build" / "last-build.json"


def checkout_dir() -> Path:
    """The Trio checkout the harness builds from: $TRIO_REPO, default <harness>/Trio (gitignored)."""
    return Path(CONFIG.get("TRIO_REPO")).expanduser().resolve()


def ensure_checkout(dest: Path | None = None) -> Path:
    """Clone Trio into the checkout dir if it is missing (fast local reference clone when TH_TRIO_REFERENCE exists)."""
    dest = dest or checkout_dir()
    if (dest / ".git").exists():
        return dest
    ref = Path(CONFIG.get("TH_TRIO_REFERENCE")).expanduser()
    origin = CONFIG.get("TH_TRIO_ORIGIN")
    url = _gh_repo_url(origin or DEFAULT_GH_REPO)
    cmd = ["clone", "--no-checkout"]
    # A working clone or a bare mirror (CI workers keep one of nightscout/Trio) both work as a reference.
    if (ref / ".git").exists() or (ref / "HEAD").exists() and (ref / "objects").is_dir():
        if not origin:
            try:
                url = _origin_url(ref)
            except HarnessError:
                pass
        cmd += ["--reference-if-able", str(ref)]
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"cloning Trio into {dest} (origin {url})")
    _git([*cmd, url, str(dest)], REPO_ROOT, timeout=1800)
    remotes = _git(["remote"], dest, quiet=True).stdout.split()
    if "upstream" not in remotes and url != _gh_repo_url(DEFAULT_GH_REPO):
        _git(["remote", "add", "upstream", _gh_repo_url(DEFAULT_GH_REPO)], dest, check=False)
    return dest


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

# Process-scoped git config, never prompting for credentials. Private submodules such as TandemKit
# need auth: by default GitHub URLs are rewritten to SSH (the user's origin already uses SSH); with
# TH_GIT_TOKEN set (CI, no SSH key) they go over HTTPS with that token instead.
GIT_ENV_SSH = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "url.git@github.com:.insteadOf",
    "GIT_CONFIG_VALUE_0": "https://github.com/",
}
# Token mode is hermetic: global/system git config is ignored, so a user-level SSH rewrite or a
# credential manager that would prompt cannot interfere. The helper reads the token from the
# environment, so it never lands in a URL, .git/config or an error message.
GIT_ENV_TOKEN = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "url.https://github.com/.insteadOf",
    "GIT_CONFIG_VALUE_0": "git@github.com:",
    "GIT_CONFIG_KEY_1": "credential.helper",
    "GIT_CONFIG_VALUE_1": '!f() { echo username=x-access-token; echo "password=$TH_GIT_TOKEN"; }; f',
}


def git_env() -> dict[str, str]:
    token = CONFIG.get("TH_GIT_TOKEN")
    return {**GIT_ENV_TOKEN, "TH_GIT_TOKEN": token} if token else dict(GIT_ENV_SSH)


def _git(args: list[str], cwd: Path, **kw):
    env = {**git_env(), **(kw.pop("env", None) or {})}
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


def _parse_gitmodules(text: str) -> dict[str, dict]:
    mods: dict[str, dict] = {}
    cur = None
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r'\[submodule "(.+)"\]', line)
        if m:
            cur = m.group(1); mods[cur] = {}
        elif "=" in line and cur:
            k, v = [x.strip() for x in line.split("=", 1)]
            mods[cur][k] = v
    return mods


def _repo_name(url_or_slug: str) -> str:
    return re.sub(r"\.git$", "", url_or_slug.rstrip("/").rsplit("/", 1)[-1]).lower()


def resolve_pr_target(pr: str, src: Path, trio_branch: str | None = None) -> dict:
    """Classify a PR: Trio itself, or one of Trio's submodules (found via .gitmodules on candidate Trio branches).

    Returns {"kind": "trio"|"submodule", "slug", "number", "submodule"?: name, "trio_branch"?: branch}.
    """
    slug, num = resolve_pr(pr)
    if _repo_name(slug) == "trio":
        return {"kind": "trio", "slug": slug, "number": num}
    candidates = [b for b in [trio_branch, CONFIG.get("TH_TRIO_BASE_BRANCH"), "dev", "feat/tandem", "main"] if b]
    _git(["fetch", "--quiet", "origin"], src, timeout=900, check=False)
    seen = []
    for br in candidates:
        if br in seen:
            continue
        seen.append(br)
        cp = _git(["show", f"origin/{br}:.gitmodules"], src, check=False, quiet=True)
        if cp.returncode != 0:
            continue
        for name, m in _parse_gitmodules(cp.stdout).items():
            if _repo_name(m.get("url", "")) == _repo_name(slug):
                return {"kind": "submodule", "slug": slug, "number": num, "submodule": name, "trio_branch": br}
    raise HarnessError(f"{slug} is not Trio and no submodule with that repo was found on branches {seen}; "
                       "pass --trio-branch <branch that contains the submodule>")


def prepare_source(*, branch: str | None = None, pr: str | None = None, repo: str | None = None,
                   in_place: Path | None = None, submodules: list[str] | None = None,
                   fetch: bool = True, trio_branch: str | None = None) -> tuple[Path, dict]:
    """Return (source_dir, git_info).

    * no branch/pr: build the checkout as-is (cloned first if missing)
    * --branch / --pr for Trio: fetched and checked out (detached) in the checkout dir
    * --pr for a submodule repo (e.g. TandemKit): Trio is checked out at the branch that pins that submodule
      (--trio-branch, else TH_TRIO_BASE_BRANCH, dev, feat/tandem, main) and the submodule is switched to the PR head
    """
    info: dict = {}
    src = Path(in_place).expanduser().resolve() if in_place else ensure_checkout()
    if not (src / "Trio.xcworkspace").exists() and not branch and not pr:
        # fresh --no-checkout clone: materialize the default branch
        _git(["checkout", "--force", CONFIG.get("TH_TRIO_BASE_BRANCH") or "dev"], src, check=False)
    if in_place or (not branch and not pr):
        if not (src / "Trio.xcworkspace").exists():
            raise HarnessError(f"{src} does not look like a Trio checkout")
        log(f"building {src} as-is (current checkout, uncommitted changes included)")
        submodules = list(submodules or [])
    else:
        base_url = _gh_repo_url(repo) if repo and "/" in repo else (repo or None)
        if base_url:
            _git(["remote", "set-url", "origin", base_url], src)
        submodules = list(submodules or [])
        if pr:
            target = resolve_pr_target(pr, src, trio_branch)
            info["pr"] = f"{target['slug']}#{target['number']}"
            try:
                meta = json.loads(run(["gh", "pr", "view", str(target["number"]), "--repo", target["slug"], "--json",
                                       "title,headRefName,headRepositoryOwner,url,body"], quiet=True).stdout)
                info["pr_title"] = meta.get("title"); info["pr_url"] = meta.get("url"); info["pr_body"] = meta.get("body")
                info["branch"] = f"{meta.get('headRepositoryOwner', {}).get('login')}:{meta.get('headRefName')}"
            except HarnessError:
                pass
            if target["kind"] == "trio":
                remote_url = _gh_repo_url(target["slug"])
                _git(["fetch", "--force", remote_url, f"pull/{target['number']}/head:refs/th/pr-{target['number']}"], src, timeout=900)
                _git(["checkout", "--force", "--detach", f"refs/th/pr-{target['number']}"], src)
            else:
                br = target["trio_branch"]
                log(f"{target['slug']}#{target['number']} is a PR on submodule {target['submodule']}; using Trio {br}")
                _git(["fetch", "--force", "origin", br], src, timeout=900)
                _git(["checkout", "--force", "--detach", f"origin/{br}"], src)
                info["trio_branch"] = br
                info["submodule_pr"] = target
                submodules.append(f"{target['submodule']}={_gh_repo_url(target['slug'])}#pull/{target['number']}/head")
        else:
            assert branch
            m = re.match(r"^(.+?)#(.+)$", branch) or re.match(r"^([\w.-]+/[\w.-]+)@(.+)$", branch)
            if m:
                url = m.group(1) if "://" in m.group(1) or m.group(1).startswith("git@") else _gh_repo_url(m.group(1))
                br = m.group(2)
            else:
                url, br = "origin", branch
            _git(["fetch", "--force", url, f"{br}:refs/th/branch"], src, timeout=900)
            _git(["checkout", "--force", "--detach", "refs/th/branch"], src)
            info["branch"] = branch
        # remove leftovers from a previous PR/branch (e.g. a submodule dir that does not exist on this branch),
        # but keep ignored files (ConfigOverride.xcconfig, Xcode user data) — the checkout may be the user's own clone
        _git(["clean", "-fd", "-e", "ConfigOverride.xcconfig"], src, quiet=True)
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

def _user_override_team(src: Path) -> str | None:
    """DEVELOPER_TEAM from a user-written ConfigOverride.xcconfig in the checkout, if any (read only, never modified)."""
    path = src / "ConfigOverride.xcconfig"
    if not path.exists():
        return None
    text = path.read_text()
    if "generated by trio-test-harness" in text:
        # leftover from an older harness version that wrote into the checkout: remove it, it is ours
        log(f"removing harness-generated {path} (settings now come from build/*.xcconfig)")
        path.unlink()
        return None
    m = re.search(r"^\s*DEVELOPER_TEAM\s*=\s*([A-Za-z0-9]{6,})\s*$", text, re.M)
    return m.group(1) if m else None


def harness_xcconfig(src: Path, target: str, *, dev_warning: bool = False) -> tuple[Path, str]:
    """Write the harness's build settings to build/override-<target>.xcconfig (inside the harness, NOT the checkout)
    and return (path, team). Passed to xcodebuild with -xcconfig, which layers on top of the project's own
    Config.xcconfig / ConfigOverride.xcconfig without touching them.

    Team precedence: the user's ConfigOverride.xcconfig in the checkout > TH_DEVELOPER_TEAM > simulator placeholder.
    When the user's file provides the team we do not override DEVELOPER_TEAM / bundle ids at all.
    """
    user_team = _user_override_team(src)
    lines = ["// written by trio-test-harness for this build; the Trio checkout is not modified"]
    if user_team:
        team = user_team
        log(f"using DEVELOPER_TEAM {team[:3]}… from the checkout's own ConfigOverride.xcconfig")
    else:
        team = CONFIG.team_for(target)
        if target == "device" and not team:
            raise HarnessError("signed device builds (iphone-usb) need a team: DEVELOPER_TEAM in the checkout's "
                               "ConfigOverride.xcconfig (your normal Trio setup) or TH_DEVELOPER_TEAM in harness.env; "
                               "rooted-vnc builds use --trollstore (fake-signed) and need neither")
        lines += [f"DEVELOPER_TEAM = {team}",
                  f"BUNDLE_IDENTIFIER = org.nightscout.{team}.trio",
                  f"TRIO_APP_GROUP_ID = group.org.nightscout.{team}.trio.trio-app-group"]
    if not dev_warning:
        lines.append("SWIFT_ACTIVE_COMPILATION_CONDITIONS = $(inherited) DEV_BRANCH_WARNING_DISABLED")
    out = CONFIG.build_dir / f"override-{target}.xcconfig"
    out.write_text("\n".join(lines) + "\n")
    return out, team


def write_config_override(src: Path, target: str, *, dev_warning: bool = False) -> str:
    """Backwards-compatible name; see harness_xcconfig. Never writes into the checkout."""
    return harness_xcconfig(src, target, dev_warning=dev_warning)[1]


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


PACKAGES_RERESOLVED = ("Package.resolved did not match the checkout's package dependencies (typically a submodule "
                       "PR that adds a package), so packages were resolved fresh instead of from the lockfile")


def resolve_packages(src: Path, scheme: str) -> str | None:
    """Resolve SwiftPM packages from Package.resolved, as Trio CI does.

    If the lockfile is only out of date, resolve again without it and return a note saying so;
    any other failure raises.
    """
    base = ["xcodebuild", "-resolvePackageDependencies", "-workspace", CONFIG.get("TH_TRIO_WORKSPACE"),
            "-scheme", scheme, "-derivedDataPath", CONFIG.get("TH_DERIVED_DATA")]
    env = {**xcode.developer_env(), "CI": "true"}
    try:
        run([*base, "-onlyUsePackageVersionsFromResolvedFile"], cwd=src, env=env, timeout=1800)
        return None
    except HarnessError as e:
        if "out-of-date resolved file" not in str(e):
            raise
    log(f"WARNING: {PACKAGES_RERESOLVED}")
    run(base, cwd=src, env=env, timeout=1800, capture=False)
    return PACKAGES_RERESOLVED


def build(src: Path, target: str, *, configuration: str = "Debug", clean: bool = False,
          git_info: dict | None = None, log_path: Path | None = None, trollstore: bool = False) -> BuildInfo:
    scheme = CONFIG.get("TH_TRIO_SCHEME")
    workspace = CONFIG.get("TH_TRIO_WORKSPACE")
    dd = Path(CONFIG.get("TH_DERIVED_DATA")).expanduser()
    env = {**xcode.developer_env(), "CI": "true"}  # CI=true skips SwiftFormat + Crashlytics phases
    if clean and dd.exists():
        shutil.rmtree(dd)
    xcconfig, team = harness_xcconfig(src, "device" if (target == "device" and not trollstore) else "sim")
    if not (dd / "SourcePackages").exists():
        note = resolve_packages(src, scheme)
        if note:
            git_info = {**(git_info or {}), "packages_note": note}
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
           "-destination", dest, "-derivedDataPath", str(dd), "-xcconfig", str(xcconfig), *COMMON_FLAGS, *signing]
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
