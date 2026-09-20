---
name: trio-checkout
description: Check out the Trio source to test — a Trio PR/branch, or a PR on one of Trio's submodules (TandemKit, LoopKit, …) which switches Trio to the branch pinning that submodule and the submodule to the PR head — into the harness checkout ($TRIO_REPO, default ~/repos/trio-test-harness/Trio). Use when asked to check out / fetch / prepare a Trio PR or submodule PR for testing.
---

# trio-checkout

`cd ~/repos/trio-test-harness`. Nothing here needs Xcode.

| Want | Command |
|---|---|
| Trio PR | `th build --pr <URL or owner/repo#N or N> --checkout-only` |
| Submodule PR (e.g. TandemKit) | `th build --pr <submodule PR URL> --checkout-only` — Trio is checked out at the branch whose `.gitmodules` pins that repo (tries `--trio-branch`, `TH_TRIO_BASE_BRANCH`, dev, feat/tandem, main), then the submodule is fetched at `pull/N/head` |
| Trio branch (origin or fork) | `th build --branch feat/tandem` · `--branch nightscout/Trio@dev` · `--branch <URL>#branch` |
| Extra submodule override | add `--submodule TandemKit@my-branch` / `TandemKit@pr/12` / `TandemKit=<URL>#branch` |
| Your own working copy as-is | `th build --in-place ~/repos/Trio --checkout-only` |

Facts: the checkout is a `--reference` clone of `TH_TRIO_REFERENCE` (~/repos/Trio) when that exists, so it is fast and
shares objects; git URLs to GitHub are rewritten to SSH (private submodules like jwoglom/tandemkit need it); submodules are
initialised recursively and verified (`submodules not initialized: […]` means a clone failed); the output JSON lists
`pr`, `pr_title`, `pr_body`, `branch`, `sha`, `submodule_overrides`, `submodule_pr`, and every submodule's commit.
Never commit or push inside this checkout; it is disposable.
