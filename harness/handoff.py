"""Carry a finished web UI run on past its sandboxed agent: the same conversation with full access, or a cloud session.

Three ways, all started from a finished job's "Continue" panel (`th webui`):

  local     `claude --resume <agent session> --fork-session --remote-control`, on this Mac, out of the sandbox:
            the agent keeps everything it saw, edits/commits/pushes in the Trio checkout (or a worktree copy of it
            at exactly the tested commits and branches), and can rebuild and re-test with the harness. It is driven
            from claude.ai/code or the Claude app through Remote Control; the page shows the link.
  cloud     a new Claude Code cloud session (`claude --cloud`) on the PR's head repository and branch, with the
            findings and every repo/branch/commit the run built written into its first message.
  followup  when the PR description links the claude.ai/code session that wrote the PR, the findings are sent
            there as a message (`claude -p ... --cloud <session>`).

Each continuation's state is in runs/.webui/<job>/continue-<n>/ (meta.json, terminal.log, url).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .build import _gh_slug, _git, split_branch_spec
from .config import CONFIG, REPO_ROOT
from .util import HarnessError, read_json, write_json

SESSION_URL = re.compile(r"https://claude\.ai/code/(session_[A-Za-z0-9]+)")
WORKTREES = CONFIG.runs_dir / ".worktrees"
PERMISSION_MODES = ("acceptEdits", "default", "auto", "plan")
SHA = re.compile(r"^[0-9a-f]{7,40}$")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ---------------- the agent's session -----------------

def _projects_dir(cwd: Path) -> Path:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return base / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def agent_session(rd: Path, recorded: str | None = None) -> str | None:
    """The Claude Code session id of the run's agent: the one the web UI gave it, else found by its first prompt
    (jobs from before --session-id was passed)."""
    proj = _projects_dir(REPO_ROOT)
    if recorded and (proj / f"{recorded}.jsonl").exists():
        return recorded
    if not proj.is_dir():
        return None
    marker = f"prepared run `{rd}`"
    best = None
    for f in proj.glob("*.jsonl"):
        try:
            with open(f, errors="replace") as fh:
                head = "".join(fh.readline() for _ in range(4))
        except OSError:
            continue
        if marker in head.replace("\\n", "\n") and (best is None or f.stat().st_mtime > best.stat().st_mtime):
            best = f
    return best.stem if best else None


# ---------------- what the run built -----------------

@dataclass
class Repo:
    name: str                 # "Trio" or the submodule path
    path: str                 # relative to the Trio checkout ("" for Trio itself)
    sha: str                  # the commit the run built
    url: str | None = None    # where its branch lives (https)
    branch: str | None = None # that branch
    pr: str | None = None     # owner/repo#N when the branch is a PR head
    local: str | None = None  # local branch set up for it (filled by setup_branches)
    remote: str | None = None # remote name that local branch tracks
    note: str = ""
    picks: tuple = ()         # cherry-picks applied on top (--cherry-pick / --submodule-cherry-pick): no tracking branch

    @property
    def slug(self) -> str | None:
        return _slug(self.url) if self.url else None


def _slug(url: str) -> str | None:
    """owner/repo of a GitHub URL, case kept (build._gh_slug lowercases, for comparing)."""
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url or "")
    return m.group(1) if m else None


def _https(url: str) -> str:
    slug = _slug(url)
    return f"https://github.com/{slug}" if slug else url


def _pr_head(slug: str, number: int | str) -> tuple[str, str] | None:
    """(head repo https URL, head branch) of a PR, via gh."""
    try:
        cp = subprocess.run(["gh", "pr", "view", str(number), "--repo", slug, "--json",
                             "headRefName,headRepository,headRepositoryOwner"],
                            capture_output=True, text=True, timeout=60)
        d = json.loads(cp.stdout) if cp.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        d = None
    if not d or not d.get("headRefName"):
        return None
    owner = (d.get("headRepositoryOwner") or {}).get("login")
    name = (d.get("headRepository") or {}).get("name") or slug.split("/")[1]
    return f"https://github.com/{owner}/{name}", d["headRefName"]


def _remote_url(repo: Path, remote: str) -> str | None:
    cp = _git(["remote", "get-url", remote], repo, quiet=True, check=False)
    return cp.stdout.strip() or None if cp.returncode == 0 else None


def _trio_branch(spec: str, src: Path) -> tuple[str | None, str]:
    """A Trio branch spec (build.split_branch_spec) -> (https URL, branch), resolving remotes of the checkout;
    a bare name is origin's if origin has it, else upstream's (as the build fetched it)."""
    where, name = split_branch_spec(spec, src)
    if "://" in where or where.startswith("git@"):
        return _https(where), name
    if where == "origin" and spec == name:
        cp = _git(["ls-remote", "--exit-code", "--heads", "origin", name], src, quiet=True, check=False, timeout=60)
        if cp.returncode != 0:
            where = "upstream" if _remote_url(src, "upstream") else ""
            if not where:
                return "https://github.com/nightscout/Trio", name
    url = _remote_url(src, where)
    return (_https(url) if url else None), name


def plan(build: dict) -> tuple[Path, list[Repo], dict[str, str]]:
    """From run.json's "build": (Trio checkout it was built in, the repos that are on a branch — Trio first, then the
    overridden submodules —, every submodule path -> the commit built)."""
    git = build.get("git") or {}
    src = Path(build.get("source_dir") or "").expanduser()
    if not (src / ".git").exists():
        raise HarnessError(f"the run's Trio checkout {src} is gone")
    subs = dict(git.get("submodules") or {})
    trio = Repo("Trio", "", git.get("sha") or "")
    pr = git.get("pr") or ""
    subpr = git.get("submodule_pr")
    if subpr and git.get("trio_branch"):
        trio.url, trio.branch = _trio_branch(git["trio_branch"], src)
    elif pr and not subpr:
        slug, num = pr.split("#")
        head = _pr_head(slug, num)
        if head:
            trio.url, trio.branch = head
            trio.pr = pr
    elif git.get("branch"):
        trio.url, trio.branch = _trio_branch(git["branch"], src)
    else:
        trio.note = "built as-is (no branch)"
    if git.get("cherry_picks"):
        trio.picks = tuple(f"{c['what']}: {c['applied']} commit(s) {', '.join(c['commits'])}" for c in git["cherry_picks"])
        trio.note = ("the tested commit is that branch plus local cherry-picks, so it has no tracking branch: push a fix "
                     "to the branch or PR it belongs to, not to this base")
    repos = [trio]
    for ov in git.get("submodule_overrides") or []:
        name, ref, url = ov.get("name"), str(ov.get("ref") or ""), ov.get("url")
        r = Repo(name, name, subs.get(name, ""))
        url = url or _remote_url(src / name, "origin")
        m = re.match(r"^(?:pull/(\d+)/head|pr/(\d+))$", ref)
        if m and url:
            slug = _slug(url) or _gh_slug(url)
            num = m.group(1) or m.group(2)
            r.pr = f"{slug}#{num}"
            head = _pr_head(slug, num)
            if head:
                r.url, r.branch = head
            else:
                r.note = "PR head branch unknown (gh pr view failed)"
        elif SHA.match(ref):
            r.note = f"pinned to commit {ref}"
        else:
            r.url, r.branch = (_https(url) if url else None), ref
        repos.append(r)
    for c in git.get("submodule_cherry_picks") or []:
        r = next((x for x in repos if x.path == c["submodule"]), None)
        if r is None:
            r = Repo(c["submodule"], c["submodule"], subs.get(c["submodule"], ""), note="at the commit Trio pins")
            repos.append(r)
        r.picks = (*r.picks, f"{c['what']}: {c['applied']} commit(s) {', '.join(c['commits'])}")
        r.note = ((r.note + "; ") if r.note else "") + ("the tested commit has local cherry-picks on top, so it has no "
                                                         "tracking branch: push fixes to the branch or PR they belong to")
    return src, repos, subs


# ---------------- working copy -----------------

def _has_commit(repo: Path, sha: str) -> bool:
    return bool(sha) and _git(["cat-file", "-e", f"{sha}^{{commit}}"], repo, quiet=True, check=False).returncode == 0


def worktree(src: Path, trio_sha: str, subs: dict[str, str], name: str) -> Path:
    """A git worktree of the Trio checkout at the built commit, each submodule a worktree of the checkout's own
    submodule repo at the commit that was built: exactly what was tested, sharing objects with the checkout (no
    downloads), and unaffected by the next run checking the fixed folder out elsewhere."""
    if not _has_commit(src, trio_sha):
        raise HarnessError(f"commit {trio_sha} is no longer in {src}; run the test again to make a copy")
    WORKTREES.mkdir(parents=True, exist_ok=True)
    dest, n = WORKTREES / name, 1
    while dest.exists():
        n += 1
        dest = WORKTREES / f"{name}-{n}"
    _git(["worktree", "add", "--detach", str(dest), trio_sha], src, quiet=True)
    missing = []
    for path in sorted(subs, key=lambda p: p.count("/")):
        sub_src, sha = src / path, subs[path]
        if not (sub_src / ".git").exists() or not _has_commit(sub_src, sha):
            missing.append(path)
            continue
        target = dest / path
        if target.exists() and any(target.iterdir()):
            shutil.rmtree(target)
        _git(["worktree", "add", "--detach", str(target), sha], sub_src, quiet=True)
    if missing:
        (dest / ".th-missing-submodules").write_text("\n".join(missing) + "\n")
    return dest


def remove_worktree(src: Path, dest: Path) -> None:
    """Remove a worktree copy made by worktree(). Refuses while any repo in it has uncommitted changes; local
    branches (and so any commits on them) stay in the checkout's repos."""
    dest = dest.resolve()
    if WORKTREES.resolve() not in dest.parents:
        raise HarnessError(f"{dest} is not a harness worktree copy")
    subs = [l.split()[1] for l in _git(["submodule", "status", "--recursive"], dest, quiet=True, check=False)
            .stdout.splitlines() if len(l.split()) >= 2]
    dirty = [p or "Trio" for p in ["", *subs] if (dest / p / ".git").exists() and
             _git(["status", "--porcelain", "--ignore-submodules=all"], dest / p, quiet=True, check=False).stdout.strip()]
    if dirty:
        raise HarnessError("uncommitted changes in " + ", ".join(dirty) + "; commit or discard them first")
    for p in sorted(subs, key=lambda x: -x.count("/")):     # git will not remove a worktree that has submodules
        if (dest / p / ".git").is_file():
            _git(["worktree", "remove", "--force", str(dest / p)], src / p, quiet=True, check=False)
    _git(["worktree", "remove", "--force", str(dest)], src, quiet=True)
    for p in ["", *subs]:
        _git(["worktree", "prune"], src / p, quiet=True, check=False)


def check_in_place(src: Path, repos: list[Repo]) -> None:
    """Working in the fixed checkout itself only makes sense while it is still at what the run built."""
    moved = []
    for r in repos:
        d = src / r.path if r.path else src
        want = r.sha
        have = _git(["rev-parse", "HEAD"], d, quiet=True, check=False).stdout.strip()
        if want and not have.startswith(want):
            moved.append(f"{r.name} is at {have[:10] or '?'}, the run built {want}")
    if moved:
        raise HarnessError("the Trio checkout has moved on since this run (another run checked it out?): "
                           + "; ".join(moved) + ". Tick \"Make repo copy\" to work on a copy at the tested commits.")


def setup_branches(root: Path, repos: list[Repo], tag: str) -> None:
    """In each repo of `root` that is on a branch: a local branch at the built commit, tracking that branch on a
    remote for its URL (added, named after the owner, if missing). The branch's own name when it is free,
    else th/<tag>/<name>."""
    for r in repos:
        if not (r.url and r.branch) or r.picks:
            continue
        d = root / r.path if r.path else root
        remotes = {}
        for line in _git(["remote", "-v"], d, quiet=True, check=False).stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                remotes.setdefault(parts[0], parts[1])
        remote = next((n for n, u in remotes.items() if _gh_slug(u) == _gh_slug(r.url)), None)
        if not remote:
            owner = (r.slug or "fork").split("/")[0].lower()
            remote, k = owner, 1
            while remote in remotes:
                k += 1
                remote = f"{owner}-{k}"
            _git(["remote", "add", remote, r.url + ".git"], d, quiet=True)
        _git(["fetch", "--quiet", remote, f"+refs/heads/{r.branch}:refs/remotes/{remote}/{r.branch}"], d,
             quiet=True, check=False, timeout=600)
        local, k = r.branch, 1
        while _git(["show-ref", "--verify", "--quiet", f"refs/heads/{local}"], d, quiet=True, check=False).returncode == 0:
            k += 1
            local = f"th/{tag}/{r.branch}" + (f"-{k - 1}" if k > 2 else "")
        _git(["checkout", "--quiet", "-b", local], d, quiet=True)
        _git(["config", f"branch.{local}.remote", remote], d, quiet=True)
        _git(["config", f"branch.{local}.merge", f"refs/heads/{r.branch}"], d, quiet=True)
        r.local, r.remote = local, remote
        tip = _git(["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{r.branch}"], d, quiet=True, check=False).stdout.strip()
        if not tip:
            r.note = (r.note + "; " if r.note else "") + f"could not fetch {remote}/{r.branch}"
        elif not tip.startswith(r.sha):
            ahead = _git(["rev-list", "--count", f"{r.sha}..{tip}"], d, quiet=True, check=False).stdout.strip()
            r.note = (r.note + "; " if r.note else "") + (
                f"{remote}/{r.branch} has {ahead} newer commit(s) than the tested {r.sha}; pull before pushing"
                if ahead and ahead != "0" else f"{remote}/{r.branch} is at {tip[:10]}, not the tested {r.sha}")


# ---------------- what the session is told -----------------

def _repo_lines(root: Path | None, repos: list[Repo], subs: dict[str, str]) -> str:
    lines = []
    for r in repos:
        where = f" at `{root / r.path if r.path else root}`" if root else ""
        br = f"branch `{r.branch}` of {r.url}" if r.branch else "no branch"
        pr = f" (PR {r.pr})" if r.pr else ""
        loc = f"; local branch `{r.local}` tracks `{r.remote}/{r.branch}`" if r.local else ""
        note = f" — {r.note}" if r.note else ""
        lines.append(f"- **{r.name}**{where}: tested commit `{r.sha}`, {br}{pr}{loc}{note}")
        lines += [f"  - cherry-picked on top: {p}" for p in r.picks]
    others = {p: s for p, s in subs.items() if p not in {r.path for r in repos if r.path}}
    if others:
        lines.append("- Other submodules, at the commits Trio pins: "
                     + ", ".join(f"{p} `{s}`" for p, s in sorted(others.items())))
    return "\n".join(lines)


def findings(job: dict, rd: Path) -> str:
    meta = read_json(rd / "run.json", {}) or {}
    git = (meta.get("build") or {}).get("git") or {}
    hl = read_json(rd / "highlights.json", None)
    verdict = hl.get("verdict") if isinstance(hl, dict) else None
    s = rd / "agent-summary.md"
    summary = s.read_text().strip() if s.exists() else ""
    head = [f"PR: {git.get('pr_url') or job.get('pr') or '(none; a branch run)'}"
            + (f" — {git['pr_title']}" if git.get("pr_title") else ""),
            f"Target: {job['inputs']['target']} ({job['inputs']['configuration']})",
            f"Run status: {meta.get('status') or job.get('status')}" + (f"; review verdict: {verdict}" if verdict else "")]
    if meta.get("summary"):
        head.append(f"Run summary: {meta['summary']}")
    return "\n".join(head) + ("\n\n### Agent findings\n\n" + summary if summary else "")


RULES = """- Screenshots and data from the harness are fake (Glucose Simulator, Pump Simulator). Never put names, emails,
  device identifiers (UDIDs, serials), Apple IDs, tokens or real CGM/pump data into code, commits, PRs or comments.
- Trio is automated insulin delivery software: call out anything that changes dosing or safety behaviour, and do not
  weaken a safety check to make a test pass.
- Push only to the branches listed above; never force-push or rewrite published history unless the person asks.
- A submodule change is two commits: commit and push in the submodule, then bump its pointer in Trio."""


def local_system_prompt(job: dict, rd: Path, workdir: Path, copy: bool, src: Path, repos: list[Repo],
                        subs: dict[str, str], mode: str, resumed: bool = True) -> str:
    target = "rooted-vnc" if job["inputs"]["target"] == "rooted-vnc" else "sim"
    where = (f"`{workdir}` is a git worktree copy of the harness's Trio checkout, made for you at exactly the commits "
             f"the run tested (the shared checkout `{src}` is left alone: another run may use it at any time)."
             if copy else
             f"`{workdir}` is the harness's own Trio checkout. It is shared: the next test run checks it out again "
             "(detached, discarding uncommitted changes), so commit and push before one starts.")
    intro = ("""The conversation so far was a sandboxed, non-interactive test run. That run is over: the person who started it pressed
"Continue conversation" and is now talking to you through Remote Control (claude.ai/code or the Claude app). You can
ask them questions again.

The run's limits are lifted.""" if resumed else f"""The person who ran a trio-test-harness test started this session from its web UI page ("Start session"; no agent
ran in that test, so there is no earlier conversation) and is talking to you through Remote Control (claude.ai/code or
the Claude app). The run is in `{rd}`: `pr.md` (what was tested), `steps.jsonl`, `report.html`. What it found:

{findings(job, rd)}
""")
    return f"""\
# {'Continued' if resumed else 'Started'} from the trio-test-harness web UI

{intro}
This is a normal interactive Claude Code session (permission mode `{mode}`): you may read and edit files anywhere the
person allows, use git and gh, build, commit and push. `$TH_RUN` points at the run directory `{rd}`, so new screenshots
and notes land there.

## Where to work

{where}

{_repo_lines(workdir, repos, subs)}

To re-test a change, build that folder with the harness and install/launch it as the run did (trio-build, trio-install,
trio-launch skills), for example
`./trio-test-harness --target {target} build --in-place {workdir}{' --trollstore' if target == 'rooted-vnc' else ''}` from `{REPO_ROOT}`.
The harness daemon may be stopped; start it with `./trio-test-harness --target {target} daemon start` if a command needs it.

## Rules

{RULES}
"""


FIRST_MESSAGE = ("(From the web UI) The test run is finished and I have continued this conversation with full access. "
                 "In a few lines: confirm the folder and branches you will work in (see the system prompt), and what "
                 "you would change or check next given your findings. Then wait for me.")


FIRST_MESSAGE_NEW = ("(From the web UI) I started this session from a finished test run (details in the system prompt). "
                     "In a few lines: confirm the folder and branches you will work in, what the run shows, and what you "
                     "would change or check next. Then wait for me.")


def cloud_prompt(job: dict, rd: Path, primary: Repo, repos: list[Repo], subs: dict[str, str]) -> str:
    return f"""\
Follow-up to an automated test of the Trio iOS app (AID software) by trio-test-harness on {job['inputs']['target']}.
This session has **{primary.slug}** checked out at branch `{primary.branch}` (the repository under test). It cannot
build or run iOS apps: changes are re-tested by the person with the harness afterwards, from the pushed branch.

## What the run built

{_repo_lines(None, repos, subs)}

Clone other repositories from GitHub if you need them (Trio: `git clone --recurse-submodules`, then check out the
commits above).

## Test results

{findings(job, rd)}

## How to proceed

Start with a short plan: what the findings mean for the code and what you would change or investigate. Wait for the
person to confirm before committing or pushing.

{RULES}
"""


def followup_prompt(job: dict, rd: Path, repos: list[Repo], subs: dict[str, str]) -> str:
    return f"""\
trio-test-harness tested this pull request on {job['inputs']['target']} (fake CGM and pump data). The results are
below. Review them against your change; propose fixes, and wait for the person before pushing.

## What the run built

{_repo_lines(None, repos, subs)}

## Test results

{findings(job, rd)}
"""


# ---------------- continuations -----------------

class Continuations:
    """The continuations of one web UI job, kept in the job's directory."""

    def __init__(self, job_dir: Path):
        self.dir = job_dir
        self.lock = threading.Lock()
        self.procs: dict[str, subprocess.Popen] = {}

    def _metas(self) -> list[tuple[Path, dict]]:
        out = []
        for d in sorted(self.dir.glob("continue-*"), key=lambda p: int(p.name.split("-")[1])):
            m = read_json(d / "meta.json", None)
            if isinstance(m, dict):
                out.append((d, m))
        return out

    def _new(self, kind: str, **kw) -> tuple[Path, dict]:
        with self.lock:
            n = 1 + max([int(d.name.split("-")[1]) for d in self.dir.glob("continue-*")] or [0])
            d = self.dir / f"continue-{n}"
            d.mkdir()
            meta = {"id": str(n), "kind": kind, "status": "starting", "created": _now(), **kw}
            write_json(d / "meta.json", meta)
            return d, meta

    @staticmethod
    def _save(d: Path, meta: dict, **kw) -> dict:
        meta.update(kw)
        tmp = d / "meta.json.tmp"
        write_json(tmp, meta)
        tmp.replace(d / "meta.json")
        return meta

    def _alive(self, cid: str, pid: int | None) -> bool:
        p = self.procs.get(cid)
        if p is not None:
            return p.poll() is None
        if not pid:
            return False
        cp = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
        return "harness.ptyrun" in cp.stdout

    def list(self) -> list[dict]:
        out = []
        for d, m in self._metas():
            url_file = d / "url"
            if not m.get("url") and url_file.exists():
                m = self._save(d, m, url=url_file.read_text().strip())
            if m.get("status") in ("starting", "live") and m.get("pid"):
                alive = self._alive(m["id"], m["pid"])
                if m["kind"] == "local":
                    if alive and m.get("url") and m["status"] != "live":
                        m = self._save(d, m, status="live")
                    elif not alive:
                        m = self._save(d, m, status="ended" if m.get("url") else "failed", ended=_now(),
                                       error=m.get("error") or ("" if m.get("url") else _tail(d / "terminal.log")))
                elif not alive:          # cloud: the CLI exits once the session exists
                    m = self._save(d, m, status="created" if m.get("url") else "failed", ended=_now(),
                                   error="" if m.get("url") else _tail(d / "terminal.log"))
            out.append(m)
        return out

    def live_local(self) -> list[dict]:
        return [m for m in self.list() if m["kind"] == "local" and m["status"] in ("starting", "live")]

    def stop(self, cid: str) -> dict:
        for d, m in self._metas():
            if m["id"] == cid:
                if m.get("pid") and self._alive(cid, m["pid"]):
                    try:
                        os.kill(m["pid"], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    p = self.procs.get(cid)
                    if p:
                        try:
                            p.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            p.kill()
                return self._save(d, m, status="ended", ended=_now())
        raise HarnessError(f"no continuation {cid}")

    def remove_copy(self, cid: str) -> dict:
        for d, m in self._metas():
            if m["id"] == cid:
                if not (m.get("copy") and m.get("workdir")) or m.get("copy_removed"):
                    raise HarnessError("this continuation has no worktree copy")
                if m.get("status") in ("starting", "live") and self._alive(cid, m.get("pid")):
                    raise HarnessError("stop the session first")
                if Path(m["workdir"]).exists():
                    remove_worktree(Path(m["src"]), Path(m["workdir"]))
                return self._save(d, m, copy_removed=_now())
        raise HarnessError(f"no continuation {cid}")

    def _spawn(self, d: Path, meta: dict, cmd: list[str], env: dict, cwd: Path, *, accept_trust: bool = False,
               exit_after_url: bool = False) -> None:
        runner = [sys.executable, "-m", "harness.ptyrun", "--log", str(d / "terminal.log"), "--url-file", str(d / "url"),
                  "--cwd", str(cwd)]
        runner += ["--accept-trust"] if accept_trust else []
        runner += ["--exit-after-url"] if exit_after_url else []
        with open(d / "runner.log", "ab") as err:
            p = subprocess.Popen([*runner, "--", *cmd], cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL,
                                 stdout=err, stderr=err, start_new_session=True)
        self.procs[meta["id"]] = p
        self._save(d, meta, pid=p.pid)

    # --- the three kinds ---

    def start_local(self, job: dict, rd: Path, env: dict, *, copy: bool, mode: str, model: str) -> dict:
        sid = agent_session(rd, job.get("agent_session"))    # None: no agent ran; a new session gets the run's details
        if mode not in PERMISSION_MODES:
            raise HarnessError(f"permission mode must be one of {', '.join(PERMISSION_MODES)}")
        d, meta = self._new("local", copy=copy, permission_mode=mode, resumed=sid)
        threading.Thread(target=self._local, args=(d, meta, job, rd, env, copy, mode, model, sid), daemon=True).start()
        return meta

    def _local(self, d, meta, job, rd, env, copy, mode, model, sid) -> None:
        try:
            build = (read_json(rd / "run.json", {}) or {}).get("build") or {}
            src, repos, subs = plan(build)
            if copy:
                self._save(d, meta, step="making the worktree copy")
                workdir = worktree(src, repos[0].sha, subs, rd.name)
            else:
                check_in_place(src, repos)
                workdir = src
            self._save(d, meta, step="setting up branches", workdir=str(workdir), src=str(src))
            setup_branches(workdir, repos, f"{rd.name}-{meta['id']}")
            (d / "repos.json").write_text(json.dumps([asdict(r) for r in repos], indent=1) + "\n")
            system = local_system_prompt(job, rd, workdir, copy, src, repos, subs, mode, resumed=bool(sid))
            (d / "system-prompt.md").write_text(system)
            pr = job.get("pr") or job["inputs"].get("trio") or "Trio"
            name = f"Trio test follow-up: {pr}"[:80]
            new_sid = None if sid else str(uuid.uuid4())       # a new session gets a known id, to resume it later
            cmd = ["claude", *(["--resume", sid, "--fork-session"] if sid else ["--session-id", new_sid]),
                   "--remote-control", name, "--add-dir", str(workdir),
                   "--permission-mode", mode, "--append-system-prompt", system]
            if model:
                cmd += ["--model", model]
            cmd.append(FIRST_MESSAGE if sid else FIRST_MESSAGE_NEW)
            self._save(d, meta, step="starting Claude Code", name=name,
                       repos=[{"name": r.name, "branch": r.branch, "local": r.local, "remote": r.remote, "note": r.note}
                              for r in repos],
                       terminal=(f"cd {REPO_ROOT} && claude --resume {sid} --fork-session --add-dir {workdir}" if sid else
                                 f"cd {REPO_ROOT} && claude --resume {new_sid} --add-dir {workdir}"))
            self._spawn(d, meta, cmd, {**env, "TH_RUN": str(rd)}, REPO_ROOT)
        except (HarnessError, OSError, subprocess.SubprocessError) as e:
            self._save(d, meta, status="failed", error=str(e)[-2000:], ended=_now())

    def start_cloud(self, job: dict, rd: Path, env: dict) -> dict:
        d, meta = self._new("cloud")
        threading.Thread(target=self._cloud, args=(d, meta, job, rd, env), daemon=True).start()
        return meta

    def _cloud(self, d, meta, job, rd, env) -> None:
        try:
            build = (read_json(rd / "run.json", {}) or {}).get("build") or {}
            _, repos, subs = plan(build)
            # the repository under test: the PR's head (a submodule PR's repo, else Trio's), else Trio's branch
            primary = next((r for r in repos if r.pr and r.url and r.branch), None) or \
                next((r for r in repos if r.url and r.branch), None)
            if not primary:
                raise HarnessError("this run was not built from a branch (nothing a cloud session could clone)")
            self._save(d, meta, step=f"fetching {primary.slug} {primary.branch}", repo=primary.slug, branch=primary.branch)
            # `claude --cloud` clones the current directory's GitHub remote at its current branch: a one-commit
            # checkout of just that is enough (inside the harness folder, gitignored under runs/)
            co = d / (primary.slug or "repo").split("/")[-1]
            co.mkdir()
            _git(["init", "--quiet", "-b", primary.branch], co, quiet=True)
            _git(["remote", "add", "origin", primary.url + ".git"], co, quiet=True)
            _git(["fetch", "--quiet", "--depth", "1", "origin", f"refs/heads/{primary.branch}"], co, quiet=True, timeout=600)
            _git(["reset", "--quiet", "--hard", "FETCH_HEAD"], co, quiet=True)
            prompt = cloud_prompt(job, rd, primary, repos, subs)
            (d / "prompt.md").write_text(prompt)
            self._save(d, meta, step="creating the cloud session")
            self._spawn(d, meta, ["claude", "--cloud", prompt], env, co, accept_trust=True, exit_after_url=True)
        except (HarnessError, OSError, subprocess.SubprocessError) as e:
            self._save(d, meta, status="failed", error=str(e)[-2000:], ended=_now())

    def send_followup(self, job: dict, rd: Path, env: dict, session: str) -> dict:
        m = SESSION_URL.search(session) or re.match(r"^(session_[A-Za-z0-9]+)$", session)
        if not m:
            raise HarnessError("not a claude.ai/code session link")
        sid = m.group(1)
        d, meta = self._new("followup", session=f"https://claude.ai/code/{sid}")
        threading.Thread(target=self._followup, args=(d, meta, job, rd, env, sid), daemon=True).start()
        return meta

    def _followup(self, d, meta, job, rd, env, sid) -> None:
        try:
            build = (read_json(rd / "run.json", {}) or {}).get("build") or {}
            _, repos, subs = plan(build)
            prompt = followup_prompt(job, rd, repos, subs)
            (d / "prompt.md").write_text(prompt)
            cp = subprocess.run(["claude", "-p", prompt, "--cloud", sid, "--output-format", "json"], cwd=REPO_ROOT,
                                env=env, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
            (d / "terminal.log").write_text(cp.stdout + cp.stderr)
            try:
                res = json.loads(cp.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                res = {}
            if cp.returncode == 0 and res.get("ok"):
                self._save(d, meta, status="sent", url=res.get("url") or meta["session"], ended=_now())
            else:
                self._save(d, meta, status="failed", ended=_now(),
                           error=(res.get("error") or cp.stderr or cp.stdout or "claude failed")[-1500:])
        except (HarnessError, OSError, subprocess.SubprocessError) as e:
            self._save(d, meta, status="failed", error=str(e)[-2000:], ended=_now())


def pr_session(rd: Path) -> str | None:
    """The claude.ai/code session linked from the PR description (a PR written by Claude Code on the web)."""
    git = ((read_json(rd / "run.json", {}) or {}).get("build") or {}).get("git") or {}
    m = SESSION_URL.search(git.get("pr_body") or "")
    return f"https://claude.ai/code/{m.group(1)}" if m else None


def _tail(path: Path, n: int = 1200) -> str:
    if not path.exists():
        return "no output"
    from .ptyrun import CSI
    text = CSI.sub(b"", path.read_bytes()[-20000:]).decode("utf8", "replace")
    return " ".join(text.split())[-n:]
