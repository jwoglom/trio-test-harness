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
import stat
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
        if not (ref / ".git").exists():
            cmd.append("--dissociate")  # a shared mirror: do not depend on it after the clone
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


def _gh_slug(url: str) -> str:
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1).lower() if m else url


GH_LINK = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/(tree|commit|commits)/([^?#\s]+?)/?(?:[?#].*)?$")


def github_link(spec: str) -> tuple[str, str, str] | None:
    """A GitHub branch or commit link -> ("branch"|"commit", owner/repo, branch or sha); else None.
    https://github.com/o/r/tree/feat/x, .../commits/feat/x, .../commit/<sha> (PR links are handled where PRs are)."""
    m = GH_LINK.match(spec.strip())
    if not m:
        return None
    slug, kind, ref = m.groups()
    if kind == "commit" or (kind == "tree" and re.fullmatch(r"[0-9a-f]{40}", ref)):
        return "commit", slug, ref
    return "branch", slug, ref


def link_as_spec(spec: str) -> str:
    """A GitHub branch/commit link -> owner/repo@<branch or sha> (the form the submodule options take)."""
    gl = github_link(spec)
    return f"{gl[1]}@{gl[2]}" if gl else spec


def split_branch_spec(spec: str, src: Path) -> tuple[str, str]:
    """A Trio branch spec -> (remote name or fetch URL, branch).

      feat/x                                  origin (falls back to nightscout/Trio if origin lacks it)
      upstream:feat/x   upstream@feat/x       a remote of the checkout (origin, upstream, ...)
      owner/repo:feat/x owner/repo@feat/x     that GitHub repo
      <git URL>#feat/x                        any repo
      https://github.com/o/r/tree/feat/x      a GitHub branch link (a /commit/<sha> link: that commit)
    Git forbids ':' in branch names, so "a:b" is never a plain branch.
    """
    if gl := github_link(spec):
        return _gh_repo_url(gl[1]), gl[2]
    m = re.match(r"^(.+?)#(.+)$", spec)
    if m:
        return (m.group(1) if "://" in m.group(1) or m.group(1).startswith("git@") else _gh_repo_url(m.group(1))), m.group(2)
    m = re.match(r"^([\w.-]+/[\w.-]+)[:@](.+)$", spec)
    if m:
        return _gh_repo_url(m.group(1)), m.group(2)
    m = re.match(r"^([\w.-]+)[:@](.+)$", spec)
    if m:
        remotes = _git(["remote"], src, quiet=True).stdout.split()
        if m.group(1) in remotes:
            return m.group(1), m.group(2)
        if ":" in spec:
            raise HarnessError(f"{spec!r}: {m.group(1)!r} is not a remote of {src} ({', '.join(remotes)}); "
                               "use owner/repo:branch for another repository")
    return "origin", spec


def _fetch_trio_branch(src: Path, br: str, ref: str) -> str:
    """Fetch Trio branch spec `br` (see split_branch_spec) into `ref`. Returns the remote/URL it came from.

    A bare name comes from origin, else from upstream / nightscout/Trio: origin is often a fork (jwoglom/Trio) that
    does not carry every upstream branch (e.g. feat/tandem-closed-beta).
    """
    url, name = split_branch_spec(br, src)
    if (url, name) != ("origin", br):
        _git(["fetch", "--force", url, f"+{name}:{ref}"], src, timeout=900)
        return url
    cp = _git(["fetch", "--force", "origin", f"+{br}:{ref}"], src, check=False, timeout=900)
    if cp.returncode == 0:
        return "origin"
    out = (cp.stderr or "") + (cp.stdout or "")
    origin = _origin_url(src)
    if "couldn't find remote ref" not in out or _gh_slug(origin) == DEFAULT_GH_REPO.lower():
        raise HarnessError(f"git fetch origin {br} failed:\n{out[-2000:]}")
    upstream = "upstream" if "upstream" in _git(["remote"], src, quiet=True).stdout.split() else _gh_repo_url(DEFAULT_GH_REPO)
    log(f"branch {br} is not on origin ({_gh_slug(origin)}); fetching it from {DEFAULT_GH_REPO}")
    _git(["fetch", "--force", upstream, f"+{br}:{ref}"], src, timeout=900)
    return DEFAULT_GH_REPO


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


def submodule_override(spec: str, src: Path) -> SubmoduleOverride:
    """--submodule spec -> override. Besides NAME@REF / NAME=URL#REF, accepts a submodule PR
    (URL or owner/repo#N) and owner/repo@BRANCH; the submodule is found in .gitmodules by repo name."""
    spec = link_as_spec(spec)
    pr = re.match(r"^(?:https://github\.com/)?([\w.-]+/[\w.-]+)(?:/pull/|#)(\d+)/?$", spec)
    branch = None if pr else re.match(r"^([\w.-]+/[\w.-]+)@(.+)$", spec)
    if not (pr or branch):
        return SubmoduleOverride.parse(spec)
    slug, ref = (pr.group(1), f"pr/{pr.group(2)}") if pr else (branch.group(1), branch.group(2))
    gm = src / ".gitmodules"
    mods = _parse_gitmodules(gm.read_text()) if gm.exists() else {}
    for name, m in mods.items():
        if _repo_name(m.get("url", "")) == _repo_name(slug):
            # owner/repo@ref names the repo: a fork's branch or commit is fetched from that fork
            same = _gh_slug(m.get("url", "")) == _gh_slug(_gh_repo_url(slug))
            return SubmoduleOverride(name, ref, None if pr or same else _gh_repo_url(slug))
    raise HarnessError(f"{slug} is not a submodule of this Trio checkout ({', '.join(sorted(mods)) or 'none'})")


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


def _slug_key(url: str) -> tuple[str, str] | None:
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    return (m.group(1).lower(), m.group(2).lower()) if m else None


def _init_submodules_from_mirrors(src: Path) -> None:
    """Clone top-level submodules with a local bare mirror as --reference, where one exists.

    TH_GIT_MIRRORS is a directory of <owner>/<repo>.git mirrors (the CI workers keep Trio's public
    submodules there). Objects come from local disk; anything newer is still fetched from GitHub.
    Submodules without a mirror, and nested ones, are left to the normal update that follows.
    """
    root = CONFIG.get("TH_GIT_MIRRORS")
    gm = src / ".gitmodules"
    if not root or not gm.exists() or not Path(root).is_dir():
        return
    mirrors = {(o.name.lower(), r.name[:-4].lower()): r
               for o in Path(root).iterdir() if o.is_dir()
               for r in o.glob("*.git") if (r / "HEAD").exists()}
    for name, m in _parse_gitmodules(gm.read_text()).items():
        path, key = m.get("path"), _slug_key(m.get("url", ""))
        mirror = mirrors.get(key) if key else None
        if not path or not mirror or (src / path / ".git").exists():
            continue
        # --dissociate (git >= 2.54) copies the borrowed objects so the clone no longer depends on the
        # mirror; older git (Apple's) keeps the reference, which is fine while the mirror is not pruned.
        base = ["submodule", "update", "--init", "--reference", str(mirror)]
        cp = _git([*base, "--dissociate", "--", path], src, check=False, quiet=True, timeout=1800)
        if cp.returncode != 0 and "dissociate" in (cp.stderr or ""):
            cp = _git([*base, "--", path], src, check=False, quiet=True, timeout=1800)
        log(f"submodule {name}: {'from mirror ' + str(mirror) if cp.returncode == 0 else 'mirror failed, will clone'}")


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
        if cp.returncode != 0 and br == trio_branch:
            # asked for by name: it may be an upstream-only branch (origin is a fork)
            try:
                _fetch_trio_branch(src, br, "refs/th/trio-branch")
                cp = _git(["show", "refs/th/trio-branch:.gitmodules"], src, check=False, quiet=True)
            except HarnessError:
                pass
        if cp.returncode != 0:
            continue
        for name, m in _parse_gitmodules(cp.stdout).items():
            if _repo_name(m.get("url", "")) == _repo_name(slug):
                return {"kind": "submodule", "slug": slug, "number": num, "submodule": name, "trio_branch": br}
    raise HarnessError(f"{slug} is not Trio and no submodule with that repo was found on branches {seen}; "
                       "pass --trio-branch <branch that contains the submodule>")


# A cherry-pick needs a committer; CI's hermetic git config has none. These commits never leave the checkout.
PICK_ENV = {"GIT_COMMITTER_NAME": "trio-test-harness", "GIT_COMMITTER_EMAIL": "trio-test-harness@localhost"}



CHERRY_PICK_HELP = ("commits to cherry-pick onto the Trio checkout, in order (repeatable): a Trio PR (link, owner/repo#N, "
                    "number) = its own commits; a commit sha; a range A..B; or a branch (feat/x, remote:feat/x, "
                    "owner/repo:feat/x) = its commits that are on neither the checkout nor the base branch")


_PR_META: dict[str, dict] = {}


def _pr_own_commits(repo: Path, slug: str, num: str, ref: str) -> list[str]:
    """A PR's own commits, oldest first, merges skipped: GitHub's list for the PR (still right once it is merged,
    when its base already contains its head). The objects come from fetching pull/N/head into `ref`."""
    _git(["fetch", "--force", _gh_repo_url(slug), f"+pull/{num}/head:{ref}"], repo, timeout=900)
    try:
        cp = run(["gh", "api", "--paginate", f"repos/{slug}/pulls/{num}/commits?per_page=100",
                  "--jq", ".[] | select((.parents | length) == 1) | .sha"], quiet=True)
    except HarnessError:
        raise HarnessError(f"{slug}#{num}: could not list the PR's commits (gh api failed); check `gh auth status`") from None
    return cp.stdout.split()


def _pick_commits(src: Path, spec: str, n: int) -> tuple[list[str], str]:
    """One --cherry-pick spec -> (commits to apply, oldest first; a description)."""
    spec = spec.strip()
    ref = f"refs/th/pick-{n}"
    pr = re.match(r"^(?:https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)/?|([\w.-]+/[\w.-]+)#(\d+)|(\d{1,6}))$", spec)
    if pr:
        slug = pr.group(1) or pr.group(3) or CONFIG.get("TH_DEFAULT_GH_REPO", DEFAULT_GH_REPO)
        num = pr.group(2) or pr.group(4) or pr.group(5)
        try:
            meta = json.loads(run(["gh", "pr", "view", num, "--repo", slug, "--json", "title,url,body"],
                                  quiet=True).stdout)
        except (HarnessError, ValueError):
            raise HarnessError(f"--cherry-pick {spec}: gh pr view failed; check `gh auth status`") from None
        commits = _pr_own_commits(src, slug, num, ref)
        _PR_META[spec] = {"pr": f"{slug}#{num}", "title": meta.get("title"), "url": meta.get("url"), "body": meta.get("body")}
        return commits, f"{slug}#{num} ({meta.get('title', '')})"
    if (gl := github_link(spec)) and gl[0] == "commit":
        if not _git(["rev-parse", "--verify", "--quiet", f"{gl[2]}^{{commit}}"], src, quiet=True, check=False).stdout.strip():
            _fetch_commit(src, gl[2], [_gh_repo_url(gl[1])])
        return [_git(["rev-parse", f"{gl[2]}^{{commit}}"], src, quiet=True).stdout.strip()], f"commit {gl[1]}@{gl[2][:10]}"
    rng = re.match(r"^([\w./^~-]+)\.\.([\w./^~-]+)$", spec)
    if rng:
        for side in rng.groups():
            if not _git(["rev-parse", "--verify", "--quiet", f"{side}^{{commit}}"], src, quiet=True, check=False).stdout.strip():
                _fetch_commit(src, side)
        commits = _git(["rev-list", "--reverse", "--no-merges", spec], src, quiet=True).stdout.split()
        return commits, f"range {spec}"
    if re.match(r"^[0-9a-f]{7,40}$", spec):
        if not _git(["rev-parse", "--verify", "--quiet", f"{spec}^{{commit}}"], src, quiet=True, check=False).stdout.strip():
            _fetch_commit(src, spec)
        sha = _git(["rev-parse", f"{spec}^{{commit}}"], src, quiet=True).stdout.strip()
        return [sha], f"commit {spec}"
    # a branch: its own commits, i.e. those neither on the checkout nor on the base branch it grew from
    _fetch_trio_branch(src, spec, ref)
    base = CONFIG.get("TH_TRIO_BASE_BRANCH") or "dev"
    excl = ["^HEAD"]
    try:
        _fetch_trio_branch(src, base, f"{ref}-base")
        excl.append(f"^{ref}-base")
    except HarnessError:
        pass
    commits = _git(["rev-list", "--reverse", "--no-merges", ref, *excl], src, quiet=True).stdout.split()
    return commits, f"branch {spec} (its commits not on {base})"


def _fetch_commit(src: Path, sha: str, extra: list[str] | None = None) -> None:
    """A commit that is not in the checkout yet: GitHub serves full shas by id; short ones need a fetch of all heads."""
    remotes = [*(extra or []), *[r for r in ("origin", "upstream") if r in _git(["remote"], src, quiet=True).stdout.split()]]
    for i, r in enumerate(remotes):
        want = [sha] if len(sha) == 40 else [f"+refs/heads/*:refs/th/heads-{i}/*"]
        _git(["fetch", "--quiet", r, *want], src, check=False, timeout=900)
        if _git(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"], src, quiet=True, check=False).stdout.strip():
            return
    raise HarnessError(f"--cherry-pick {sha}: commit not found in the checkout or on {', '.join(remotes) or 'its remotes'}"
                       " (give the full 40-character sha, or owner/repo:branch / a PR instead)")


def _apply_picks(repo: Path, commits: list[str], spec: str, what: str, start: str, flag: str = "--cherry-pick") -> dict:
    """Cherry-pick `commits` onto `repo`'s HEAD. A conflict aborts, puts `repo` back at `start` and stops the build."""
    if not commits:
        raise HarnessError(f"{flag} {spec}: no commits to apply ({what})")
    before = _git(["rev-parse", "HEAD"], repo, quiet=True).stdout.strip()
    todo = [c for c in commits
            if _git(["merge-base", "--is-ancestor", c, "HEAD"], repo, quiet=True, check=False).returncode != 0]
    log(f"cherry-picking {len(todo)} commit(s) into {repo.name}: {what}"
        + (f" ({len(commits) - len(todo)} already in its history)" if len(todo) < len(commits) else ""))
    cp = (_git(["cherry-pick", "--empty=drop", "--allow-empty", *todo], repo, check=False, env=PICK_ENV, timeout=900)
          if todo else None)
    if cp is None:
        return {"spec": spec, "what": what, "commits": [c[:10] for c in commits], "applied": 0,
                "dropped": len(commits), **({"pr": _PR_META[spec]} if spec in _PR_META else {})}
    if cp.returncode != 0:
        stuck = _git(["rev-parse", "--short=10", "CHERRY_PICK_HEAD"], repo, quiet=True, check=False).stdout.strip()
        files = _git(["diff", "--name-only", "--diff-filter=U"], repo, quiet=True, check=False).stdout.split()
        subject = _git(["log", "-1", "--format=%s", stuck], repo, quiet=True, check=False).stdout.strip() if stuck else ""
        _git(["cherry-pick", "--abort"], repo, quiet=True, check=False)
        _git(["checkout", "--force", "--detach", start], repo, quiet=True, check=False)
        raise HarnessError(f"{flag} {spec} does not apply cleanly: "
                           + (f"commit {stuck} ({subject})" if stuck else "a commit") + " conflicts"
                           + (f" in {', '.join(files)}" if files else "") + f"\n{(cp.stderr or cp.stdout)[-1500:]}")
    applied = int(_git(["rev-list", "--count", f"{before}..HEAD"], repo, quiet=True).stdout.strip() or 0)
    return {"spec": spec, "what": what, "commits": [c[:10] for c in commits], "applied": applied,
            "dropped": len(commits) - applied, **({"pr": _PR_META[spec]} if spec in _PR_META else {})}


def cherry_pick(src: Path, specs: list[str]) -> list[dict]:
    """Apply each --cherry-pick spec onto the Trio checkout's HEAD, in order. Commits already there are dropped; a
    conflict stops the build with the commit and files named, and leaves the checkout at the commit it started from."""
    start = _git(["rev-parse", "HEAD"], src, quiet=True).stdout.strip()
    out = []
    for n, spec in enumerate(specs, 1):
        commits, what = _pick_commits(src, spec, n)
        out.append(_apply_picks(src, commits, spec, what, start))
    return out


SUBMODULE_CHERRY_PICK_HELP = (
    "commits to cherry-pick onto a submodule, after the overrides, in order (repeatable): a submodule PR (link or "
    "owner/repo#N) = its own commits; NAME@sha or NAME@A..B; or NAME@branch = its commits on neither the submodule's "
    "checkout nor its default branch. NAME is the submodule (TandemKit) or owner/repo (that repo's branch or commits)")


def _find_submodule(src: Path, key: str) -> str:
    """Submodule path for a submodule name or a repo (owner/repo or its name), via .gitmodules."""
    gm = src / ".gitmodules"
    mods = _parse_gitmodules(gm.read_text()) if gm.exists() else {}
    k = key.lower()
    for name, m in mods.items():
        if name.lower() == k or m.get("path", "").lower() == k or _repo_name(m.get("url", "")) == _repo_name(k):
            return m.get("path") or name
    raise HarnessError(f"{key} is not a submodule of this Trio checkout ({', '.join(sorted(mods)) or 'none'})")


def _remote_default_branch(repo: Path, url: str) -> str | None:
    cp = _git(["ls-remote", "--symref", url, "HEAD"], repo, quiet=True, check=False, timeout=120)
    m = re.search(r"^ref: refs/heads/(\S+)\s+HEAD", cp.stdout, re.M)
    return m.group(1) if m else None


def _sub_pick_commits(src: Path, spec: str, n: int) -> tuple[str, list[str], str]:
    """One --submodule-cherry-pick spec -> (submodule path, commits to apply oldest first, a description)."""
    spec = link_as_spec(spec.strip())
    ref = f"refs/th/subpick-{n}"
    pr = re.match(r"^(?:https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)/?|([\w.-]+/[\w.-]+)#(\d+))$", spec)
    if pr:
        slug, num = pr.group(1) or pr.group(3), pr.group(2) or pr.group(4)
        path = _find_submodule(src, slug)
        sub = src / path
        try:
            meta = json.loads(run(["gh", "pr", "view", num, "--repo", slug, "--json", "title,url,body"],
                                  quiet=True).stdout)
        except (HarnessError, ValueError):
            raise HarnessError(f"--submodule-cherry-pick {spec}: gh pr view failed; check `gh auth status`") from None
        commits = _pr_own_commits(sub, slug, num, ref)
        _PR_META[spec] = {"pr": f"{slug}#{num}", "title": meta.get("title"), "url": meta.get("url"), "body": meta.get("body")}
        return path, commits, f"{slug}#{num} ({meta.get('title', '')})"
    m = re.match(r"^((?:[\w.-]+/)?[\w.-]+)@(.+)$", spec)
    if not m:
        raise HarnessError(f"--submodule-cherry-pick {spec!r}: use a PR link, owner/repo#N, NAME@sha, NAME@A..B or "
                           "NAME@branch")
    key, what_ref = m.groups()
    path = _find_submodule(src, key)
    sub = src / path
    url = _gh_repo_url(key) if "/" in key else _origin_url(sub)
    extra = [url] if "/" in key else []
    rng = re.match(r"^([\w./^~-]+)\.\.([\w./^~-]+)$", what_ref)
    if rng or re.match(r"^[0-9a-f]{7,40}$", what_ref):
        for side in (rng.groups() if rng else [what_ref]):
            if not _git(["rev-parse", "--verify", "--quiet", f"{side}^{{commit}}"], sub, quiet=True, check=False).stdout.strip():
                _fetch_commit(sub, side, extra)
        if rng:
            return path, _git(["rev-list", "--reverse", "--no-merges", what_ref], sub, quiet=True).stdout.split(), \
                f"{path} range {what_ref}"
        return path, [_git(["rev-parse", f"{what_ref}^{{commit}}"], sub, quiet=True).stdout.strip()], f"{path} commit {what_ref}"
    # a branch: its own commits, i.e. those neither on the submodule's checkout nor on the repo's default branch
    _git(["fetch", "--force", url, f"+refs/heads/{what_ref}:{ref}"], sub, timeout=900)
    excl = ["^HEAD"]
    if (default := _remote_default_branch(sub, url)) and default != what_ref:
        if _git(["fetch", "--force", url, f"+refs/heads/{default}:{ref}-base"], sub, check=False, timeout=900).returncode == 0:
            excl.append(f"^{ref}-base")
    commits = _git(["rev-list", "--reverse", "--no-merges", ref, *excl], sub, quiet=True).stdout.split()
    return path, commits, f"{path} branch {what_ref}" + (f" (its commits not on {default})" if default else "")


def submodule_cherry_pick(src: Path, specs: list[str]) -> list[dict]:
    """Apply each --submodule-cherry-pick spec onto its submodule's HEAD, in order (see _sub_pick_commits)."""
    starts: dict[str, str] = {}
    out = []
    for n, spec in enumerate(specs, 1):
        path, commits, what = _sub_pick_commits(src, spec, n)
        sub = src / path
        starts.setdefault(path, _git(["rev-parse", "HEAD"], sub, quiet=True).stdout.strip())
        out.append({"submodule": path, **_apply_picks(sub, commits, spec, what, starts[path], "--submodule-cherry-pick")})
    return out


def _module_gitdirs(src: Path) -> list[Path]:
    """The checkout's submodule git dirs (.git/modules/<name>, nested .../modules/<name>), not their worktrees/."""
    root = src / ".git" / "modules"
    if not root.is_dir():
        return []
    return sorted(p.parent for p in root.rglob("HEAD")
                  if (p.parent / "config").is_file() and "worktrees" not in p.parent.relative_to(root).parts)


def isolate_submodule_worktrees(src: Path) -> None:
    """Keep each submodule's core.worktree per-worktree (extensions.worktreeConfig + config.worktree). Without this,
    `git submodule update` inside a worktree copy (handoff.worktree) rewrites core.worktree in the submodule's shared
    config to point at the copy, and every submodule command in the checkout then fails ("cannot chdir to …")."""
    for gd in _module_gitdirs(src):
        cfg = gd / "config"
        wt = _git(["config", "--file", str(cfg), "core.worktree"], src, quiet=True, check=False).stdout.strip()
        if not wt:
            continue
        _git(["config", "--file", str(cfg), "extensions.worktreeConfig", "true"], src, quiet=True)
        _git(["config", "--file", str(gd / "config.worktree"), "core.worktree", wt], src, quiet=True)
        _git(["config", "--file", str(cfg), "--unset", "core.worktree"], src, quiet=True)


def repair_submodule_worktrees(src: Path) -> None:
    """Point submodule core.worktree settings that a worktree copy (runs/.worktrees/<copy>/<path>) hijacked back at
    the checkout's own <path>."""
    marker = f"/{CONFIG.runs_dir.name}/.worktrees/"
    for gd in _module_gitdirs(src):
        for cfg in (gd / "config", gd / "config.worktree"):
            if not cfg.is_file():
                continue
            wt = _git(["config", "--file", str(cfg), "core.worktree"], src, quiet=True, check=False).stdout.strip()
            if marker not in wt:
                continue
            rest = wt.split(marker, 1)[1].split("/", 1)[1:]     # drop the copy's folder name
            target = src / rest[0] if rest else None
            if not target or not (target / ".git").is_file():
                continue
            rel = os.path.relpath(target, gd)
            log(f"repairing {gd.relative_to(src)}: core.worktree pointed at a worktree copy, now {rel}")
            _git(["config", "--file", str(cfg), "core.worktree", rel], src, quiet=True)


# ---------------- stable mtimes -----------------

MTIME_EPOCH = 1262304000   # 2010-01-01T00:00:00Z: blob-derived mtimes land in 2010..2018, always in the past


def blob_mtime(sha: str) -> int:
    """A timestamp that depends only on a blob's content, so the same bytes get the same mtime in every checkout."""
    return MTIME_EPOCH + int(sha[:7], 16)


def stabilize_mtimes(src: Path) -> int:
    """Stamp every clean tracked file in the checkout and its submodules with an mtime derived from its git blob hash.

    git gives every file it writes the current time, and each run rewrites files that end up byte-identical (the
    submodule reset before an override, a cherry-pick re-applied onto its base). Xcode's build system and the Swift
    driver take the new mtime as a change, the dependency cascade then recompiles the whole Trio module even though
    nothing differs. With the mtime keyed on the blob hash, identical content looks untouched in every build while
    changed content (a different blob) still gets a different mtime. Files with uncommitted changes keep their real
    mtime. TH_STABLE_MTIMES=0 disables it. Returns the number of files re-stamped."""
    if not CONFIG.bool("TH_STABLE_MTIMES"):
        return 0
    from concurrent.futures import ThreadPoolExecutor
    # one listing for the checkout and every nested submodule: "mode sha stage\tpath", paths relative to src
    entries = _git(["ls-files", "-s", "-z", "--recurse-submodules"], src, quiet=True).stdout.split("\0")
    repos = [src]
    files: list[tuple[Path, int]] = []
    for entry in entries:
        meta, _, rel = entry.partition("\t")
        parts = meta.split()
        if len(parts) != 3 or parts[2] != "0":
            continue
        if parts[0] == "160000":              # a submodule that is not checked out (recursion stops there)
            continue
        if parts[0] in ("100644", "100755"):  # not symlinks
            files.append((src / rel, blob_mtime(parts[1])))
    # every checked-out submodule, from .gitmodules (git submodule status --recursive takes seconds here)
    pending = [src]
    while pending:
        repo = pending.pop()
        gm = repo / ".gitmodules"
        if not gm.is_file():
            continue
        for m in _parse_gitmodules(gm.read_text()).values():
            sub = repo / m["path"] if m.get("path") else None
            if sub and (sub / ".git").exists():
                repos.append(sub)
                pending.append(sub)

    def dirty_files(repo: Path) -> set[Path]:
        """Files whose content differs from the index (git status refreshes the stat cache as a side effect)."""
        out = _git(["status", "--porcelain", "-z", "--untracked-files=no", "--ignore-submodules=all"], repo,
                   quiet=True, check=False).stdout
        return {repo / e[3:] for e in out.split("\0") if len(e) > 3}

    with ThreadPoolExecutor(max_workers=4) as pool:
        dirty = set().union(*pool.map(dirty_files, repos))
    n = 0
    for path, t in files:
        if path in dirty:
            continue                          # uncommitted changes keep their real mtime
        ns = t * 10**9
        try:
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_mtime_ns == ns:
                continue
            os.utime(path, ns=(ns, ns), follow_symlinks=False)
        except OSError:
            continue
        n += 1
    if n:
        # re-stamped files no longer match git's cached stat data; refresh so later git commands do not re-hash them
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda r: _git(["update-index", "-q", "--refresh"], r, quiet=True, check=False), repos))
    return n


def prepare_source(*, branch: str | None = None, pr: str | None = None, repo: str | None = None,
                   in_place: Path | None = None, submodules: list[str] | None = None,
                   fetch: bool = True, trio_branch: str | None = None,
                   cherry_picks: list[str] | None = None,
                   submodule_cherry_picks: list[str] | None = None) -> tuple[Path, dict]:
    """Return (source_dir, git_info).

    * no branch/pr: build the checkout as-is (cloned first if missing)
    * --branch / --pr for Trio: fetched and checked out (detached) in the checkout dir
    * --pr for a submodule repo (e.g. TandemKit): Trio is checked out at the branch that pins that submodule
      (--trio-branch, else TH_TRIO_BASE_BRANCH, dev, feat/tandem, main) and the submodule is switched to the PR head
    * submodule_cherry_picks (--submodule-cherry-pick): applied onto submodules after the overrides
    * cherry_picks (--cherry-pick): applied in order onto whichever Trio was checked out (see cherry_pick);
      with no branch/PR they need one, so TH_TRIO_BASE_BRANCH is checked out first
    """
    info: dict = {}
    if (cherry_picks or submodule_cherry_picks) and in_place:
        raise HarnessError("cherry-picks need a branch or PR checkout, not --in-place (commit into that folder instead)")
    if cherry_picks and not branch and not pr:
        branch = CONFIG.get("TH_TRIO_BASE_BRANCH") or "dev"
    src = Path(in_place).expanduser().resolve() if in_place else ensure_checkout()
    repair_submodule_worktrees(src)
    isolate_submodule_worktrees(src)
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
                _fetch_trio_branch(src, br, "refs/th/trio-branch")
                _git(["checkout", "--force", "--detach", "refs/th/trio-branch"], src)
                info["trio_branch"] = br
                info["submodule_pr"] = target
                submodules.append(f"{target['submodule']}={_gh_repo_url(target['slug'])}#pull/{target['number']}/head")
        else:
            assert branch
            _fetch_trio_branch(src, branch, "refs/th/branch")
            _git(["checkout", "--force", "--detach", "refs/th/branch"], src)
            info["branch"] = branch
        # remove leftovers from a previous PR/branch (e.g. a submodule dir that does not exist on this branch),
        # but keep ignored files (ConfigOverride.xcconfig, Xcode user data) — the checkout may be the user's own clone
        _git(["clean", "-fd", "-e", "ConfigOverride.xcconfig"], src, quiet=True)
        if cherry_picks:
            info["cherry_picks"] = cherry_pick(src, cherry_picks)
    if fetch:
        log("syncing git submodules (first run downloads LoopKit & friends; several minutes)")
        _git(["submodule", "sync", "--recursive"], src, quiet=True)
        _init_submodules_from_mirrors(src)
        _git(["submodule", "update", "--init", "--recursive", "--jobs", "6"], src, timeout=3600)
        _check_submodules(src)
    for spec in submodules or []:
        ov = submodule_override(spec, src)
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
    if submodule_cherry_picks:
        info["submodule_cherry_picks"] = submodule_cherry_pick(src, submodule_cherry_picks)
    n = stabilize_mtimes(src)
    if n:
        log(f"stable mtimes: {n} files stamped from their git blob hashes (identical content keeps its old mtime, "
            "so xcodebuild recompiles only real changes)")
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


def harness_xcconfig(src: Path, target: str, *, dev_warning: bool = False,
                     conditions: tuple[str, ...] = (), settings: dict[str, str] | None = None) -> tuple[Path, str]:
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
    active = (*(() if dev_warning else ("DEV_BRANCH_WARNING_DISABLED",)), *conditions)
    if active:
        lines.append(f"SWIFT_ACTIVE_COMPILATION_CONDITIONS = $(inherited) {' '.join(active)}")
    lines += [f"{k} = {v}" for k, v in (settings or {}).items()]
    # one file per variant, so a stress build (extra conditions) never rewrites the plain build's settings
    suffix = "".join(f"-{c.lower()}" for c in conditions) + ("-sanitized" if settings else "")
    out = CONFIG.build_dir / f"override-{target}{suffix}.xcconfig"
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


# `path:line:col: error: …`, `xcodebuild: error: …`, `error: …`; not a quoted source line such as `var error: Error?`.
DIAGNOSTIC_ERROR = re.compile(r"(?:^|: )error: ")


class BuildTimeout(HarnessError):
    """xcodebuild was stopped at its deadline; DerivedData keeps what it compiled, so the next build resumes."""


# Build settings rather than xcodebuild's -enableThreadSanitizer: Trio's scheme embeds a watchOS app, and the sanitizers do
# not support watchOS, so they are switched on for the iOS simulator SDK only.
SANITIZER_SETTINGS = {"thread": {"ENABLE_THREAD_SANITIZER[sdk=iphonesimulator*]": "YES"},
                      "address": {"ENABLE_ADDRESS_SANITIZER[sdk=iphonesimulator*]": "YES"}}


def build(src: Path, target: str, *, configuration: str = "Debug", clean: bool = False,
          git_info: dict | None = None, log_path: Path | None = None, trollstore: bool = False,
          conditions: tuple[str, ...] = (), sanitizer: str | None = None, save: bool = True,
          timeout: float | None = None) -> BuildInfo:
    """`conditions` are extra Swift compilation conditions (e.g. TANDEMKIT_VIRTUAL_PUMP), `sanitizer` is thread or
    address, `save=False` leaves build/last-build.json (what `th install` installs) alone, and `timeout` (seconds)
    stops xcodebuild with BuildTimeout instead of letting a CI job's limit kill it without a report."""
    scheme = CONFIG.get("TH_TRIO_SCHEME")
    workspace = CONFIG.get("TH_TRIO_WORKSPACE")
    dd = Path(CONFIG.get("TH_DERIVED_DATA")).expanduser()
    env = {**xcode.developer_env(), "CI": "true"}  # CI=true skips SwiftFormat + Crashlytics phases
    if clean and dd.exists():
        shutil.rmtree(dd)
    if sanitizer and sanitizer not in SANITIZER_SETTINGS:
        raise HarnessError(f"unknown sanitizer {sanitizer!r}; use {', '.join(SANITIZER_SETTINGS)}")
    xcconfig, team = harness_xcconfig(src, "device" if (target == "device" and not trollstore) else "sim",
                                      conditions=conditions, settings=SANITIZER_SETTINGS.get(sanitizer or ""))
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
    log(f"xcodebuild ({target}, {configuration}"
        + "".join(f", {x}" for x in (*conditions, *([f"{sanitizer} sanitizer"] if sanitizer else []))) + f") -> {dd}")
    log_path = log_path or (REPO_ROOT / "build" / f"xcodebuild-{target}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    with open(log_path, "w") as fh:
        import subprocess
        import threading
        p = subprocess.Popen(cmd, cwd=src, env={**os.environ, **env}, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=timeout is not None)
        timer = None
        if timeout is not None:
            def stop():
                nonlocal timed_out
                if p.poll() is not None:
                    return
                timed_out = True
                log(f"xcodebuild still running after {timeout / 60:.0f} min; stopping it (DerivedData keeps its progress)")
                try:
                    os.killpg(p.pid, 15)
                except ProcessLookupError:
                    pass
            timer = threading.Timer(max(timeout, 1), stop)
            timer.start()
        tail: list[str] = []
        stream = CONFIG.bool("TH_BUILD_LOG_STDOUT")   # `th webui` streams the whole build into its job log
        for line in p.stdout:  # type: ignore[union-attr]
            fh.write(line)
            tail.append(line)
            if len(tail) > 60:
                tail.pop(0)
            if stream or DIAGNOSTIC_ERROR.search(line) or re.search(r"BUILD SUCCEEDED|BUILD FAILED|\*\* ", line):
                print(line.rstrip(), flush=True)
        p.wait()
        if timer:
            timer.cancel()
    if timed_out:
        raise BuildTimeout(f"xcodebuild stopped after {timeout / 60:.0f} min; full log: {log_path}")
    if p.returncode != 0:
        errs = [l for l in Path(log_path).read_text().splitlines() if DIAGNOSTIC_ERROR.search(l)]
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
    if save:
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
