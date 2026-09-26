"""Turn the e2e workflow's inputs into trio-test-e2e arguments.

    IN_TRIO="<Trio PR or branch>" IN_SUBMODULES="<PR links or NAME@branch ...>" IN_CHERRY_PICK="<PRs, shas, A..B, branches>" \
        python ci/inputs.py ARGS_FILE

Writes one argument per line to ARGS_FILE and prints `pr=` / `pr_owner=` lines for $GITHUB_OUTPUT.
The PR that trio-test-e2e is pointed at (and that a comment would go to) is the Trio PR if one was
given, else the first submodule PR (else the first cherry-picked PR, for the comment only); a Trio branch next to a submodule PR becomes --trio-branch. Each cherry-pick item
becomes --cherry-pick (applied in order onto the Trio that is checked out; see harness/build.py cherry_pick).
"""
from __future__ import annotations

import os
import re
import sys

PR_URL = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)/?$")
PR_SHORT = re.compile(r"^([\w.-]+)/([\w.-]+)#(\d+)$")
# branch, remote:branch / remote@branch, owner/repo:branch / owner/repo@branch (harness/build.py split_branch_spec)
BRANCH = re.compile(r"^(?:[\w.-]+(?:/[\w.-]+)?[:@])?[\w./-]+$")
SUB_BRANCH = re.compile(r"^(?:[\w.-]+/)?[\w.-]+@[\w./-]+$")   # NAME@branch or owner/repo@branch
# GitHub branch/commit links (harness/build.py github_link): https://github.com/o/r/tree/<branch>, .../commit/<sha>
GH_LINK = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/(?:tree|commits?)/([^?#\s]+?)/?(?:[?#]\S*)?$")
PICK_RANGE = re.compile(r"^[\w./^~-]+\.\.[\w./^~-]+$")


class InputError(ValueError):
    pass


def pr_owner(ref: str) -> str | None:
    """Owner of a PR reference, or None if it is not one. A bare number means nightscout/Trio."""
    if ref.isdigit():
        return "nightscout"
    m = PR_URL.match(ref) or PR_SHORT.match(ref)
    return m.group(1) if m else None


SUB_PICK = re.compile(r"^(?:[\w.-]+/)?[\w.-]+@[\w./^~-]+(?:\.\.[\w./^~-]+)?$")   # NAME@sha|A..B|branch


def _as_sub(token: str) -> str:
    """A GitHub branch/commit link given for a submodule -> owner/repo@<branch or sha>."""
    m = GH_LINK.match(token)
    return f"{m.group(1)}@{m.group(2)}" if m else token


def plan(trio: str, submodules: str, cherry_pick: str = "", submodule_cherry_pick: str = "") -> tuple[list[str], str | None]:
    trio = trio.strip()
    picks = [t for t in re.split(r"[\s,]+", cherry_pick.strip()) if t]
    for t in picks:
        if not (t.isdigit() or pr_owner(t) or PICK_RANGE.match(t) or BRANCH.match(t) or GH_LINK.match(t)):
            raise InputError(f"cherry-pick {t!r}: use a Trio PR (link, owner/repo#N, number), a commit sha, A..B, "
                             "or a branch")
    sub_picks = [_as_sub(t) for t in re.split(r"[\s,]+", submodule_cherry_pick.strip()) if t]
    for t in sub_picks:
        if t.isdigit() or not (pr_owner(t) or SUB_PICK.match(t)):
            raise InputError(f"submodule cherry-pick {t!r}: use a submodule PR (link, owner/repo#N), NAME@sha, "
                             "NAME@A..B or NAME@branch")
    subs = [_as_sub(t) for t in re.split(r"[\s,]+", submodules.strip()) if t]
    sub_prs, sub_branches = [], []
    for t in subs:
        if t.isdigit():
            raise InputError(f"submodule {t!r}: a bare number is ambiguous; use a PR link or owner/repo#N")
        if pr_owner(t):
            sub_prs.append(t)
        elif SUB_BRANCH.match(t):
            sub_branches.append(t)
        else:
            raise InputError(f"submodule {t!r}: use a PR link, owner/repo#N, NAME@branch or owner/repo@branch")

    args: list[str] = []
    primary = None
    if trio and pr_owner(trio):
        primary = trio
        args += ["--pr", trio]
        rest = sub_prs
    elif trio:
        if not (BRANCH.match(trio) or GH_LINK.match(trio)):
            raise InputError(f"Trio {trio!r}: not a PR reference or a branch name")
        if sub_prs:
            primary, rest = sub_prs[0], sub_prs[1:]
            args += ["--pr", primary, "--trio-branch", trio]
        else:
            rest = []
            args += ["--branch", trio]
    else:
        primary, rest = (sub_prs[0], sub_prs[1:]) if sub_prs else (None, [])
        if primary:
            args += ["--pr", primary]
    for t in rest + sub_branches:
        args += ["--submodule", t]
    for t in picks:
        args += ["--cherry-pick", t]
    for t in sub_picks:
        args += ["--submodule-cherry-pick", t]
    # a branch with a cherry-picked PR on top is a test of that PR: it gets the comment
    primary = primary or next((t for t in picks + sub_picks if pr_owner(t)), None)
    return args, primary


def main() -> int:
    try:
        args, primary = plan(os.environ.get("IN_TRIO", ""), os.environ.get("IN_SUBMODULES", ""),
                             os.environ.get("IN_CHERRY_PICK", ""), os.environ.get("IN_SUBMODULE_CHERRY_PICK", ""))
    except InputError as e:
        print(f"::error::{e}")
        return 1
    with open(sys.argv[1], "w") as f:
        f.write("\n".join(args) + ("\n" if args else ""))
    print(f"pr={primary or ''}")
    print(f"pr_owner={pr_owner(primary) if primary else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
