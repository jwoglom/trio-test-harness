# trio-test-harness

Tooling + Claude Code skills to **build, install, drive and screenshot the Trio iOS app** on either
the iOS Simulator or a USB-attached iPhone, from any Trio branch / PR / submodule branch, and to
turn the screenshots into an HTML report (claude.ai Artifact) or a GitHub PR comment.

```
./trio-test-e2e --mode simulator --pr https://github.com/nightscout/Trio/pull/1234     # Trio PR
./trio-test-e2e --mode rooted-vnc --pr https://github.com/jwoglom/TandemKit/pull/395          # submodule PR (Trio @ feat/tandem)
./trio-test-e2e                                                                        # prompts for mode and PR/branch
```
`./trio-test-e2e` runs checkout → build → install → launch (onboarding pre-seeded) → home screen → Glucose Simulator +
Pump Simulator → `report.html`, writes `runs/<run>/pr.md` with the PR description and a test-plan stub, then hands off to
Claude Code (from a terminal) or to the `trio-test-e2e` skill (inside Claude) for the PR-specific testing, the report artifact
and the optional PR comment. Every stage is also a `th` command and a Claude skill:

```
th build --pr <url> --checkout-only        # trio-checkout
th --target sim build --pr <url>           # trio-build
th --target sim install --reinstall        # trio-install
th --target sim launch --preseed-onboarding  # trio-launch (or: th flow onboarding-walk)
th flow add-cgm-sim ; th flow add-pump-sim # trio-fake-devices
th shot / tap-text / type / find / tree    # trio-ui-drive
th run finish passed --summary "..." ; th report html ; th report pr <url> --artifact-url <link>   # trio-report
```

## How it works

Three connection modes (`--mode` / `--target`):

| Piece | `simulator` | `rooted-vnc` (jailbroken iPhone) | `iphone-usb` (any iPhone, no root) |
|---|---|---|---|
| Build | `xcodebuild`, ad-hoc signing, same flags as Trio CI | unsigned build fake-signed with `codesign -s -` → `Trio.ipa` | automatic signing (`TH_DEVELOPER_TEAM`, Apple ID in Xcode) |
| Install / launch | `simctl install/launch` | `pymobiledevice3 apps install` via **AppSync Unified**; `uiopen` over SSH | `xcrun devicectl` install; launch via Spotlight (⌘3) in the mirror |
| Screenshots | `simctl io screenshot` | **TrollVNC** framebuffer (native px) over Wi-Fi or USB (`iproxy`) | **iPhone Mirroring** window capture (phone-harness, MIT) |
| Taps / typing | **WebDriverAgent** | VNC pointer/keyboard (right-click Home, middle Power) | HID-level CGEvents into the mirror window |
| Find elements | accessibility tree (`th tree`, `th tap-text`) | tesseract OCR | Apple Vision OCR (phone-harness) |
| Skip onboarding | write `Documents/onboardingCompleted.plist` | same, over SSH (+ Crashlytics off) | not possible → `th flow onboarding-walk` |
| Diagnostics | `th sim …`, `th wda …` | `th device ssh/diag/vnc-restart/unlock` | `th device mirror-status`; iPhone Mirroring must be paired and the phone locked |

All coordinates the CLI accepts are **screenshot pixels**, so an agent can look at a PNG and tap what it sees on both targets.

Python package `harness/` (CLIs `trio-test-e2e`, `bin/th`), Claude skills in `.claude/skills/` (symlinked into `~/.claude/skills` by `install.sh`):

- `trio-test-e2e` — runs `trio-test-e2e`, then the PR-specific testing + report + comment prompt
- `trio-test` — the chained workflow, stage by stage, pointing at the per-stage skills
- per stage: `trio-sim-setup`, `trio-phone-setup`, `trio-checkout`, `trio-build`, `trio-install`, `trio-launch`,
  `trio-fake-devices`, `trio-pr-plan`, `trio-ui-drive`, `trio-report`

Signing settings never touch the checkout: they are passed with `xcodebuild -xcconfig build/override-<target>.xcconfig`; a
user-written `ConfigOverride.xcconfig` in the checkout is read for its `DEVELOPER_TEAM` and otherwise left alone.

Source selection: `TRIO_REPO` is the checkout the harness uses (this machine: `~/repos/Trio`; default elsewhere:
`<harness>/Trio`, cloned on demand with `TH_TRIO_REFERENCE` as a git reference). A PR on a submodule repo is detected
through `.gitmodules` of the candidate Trio branches (`--trio-branch`, `TH_TRIO_BASE_BRANCH`, dev, feat/tandem, main).
`--cherry-pick SPEC` (repeatable; "Add cherry-pick" under the Trio field in the web UI, `cherry_pick` in CI) applies
commits onto whatever Trio was checked out, in order, locally. A SPEC is a Trio PR (its own commits as GitHub lists them,
merges skipped; this works for merged PRs too), a commit sha, a range `A..B`, or a branch (its commits on neither the
checkout nor `TH_TRIO_BASE_BRANCH`). For example, `--branch upstream:feat/tandem-closed-beta --cherry-pick
nightscout/Trio#1580` tests a dev PR on the branch that has TandemKit. `--submodule-cherry-pick SPEC` does the same for a
submodule, after the overrides: a submodule PR (link or `owner/repo#N`), `NAME@sha`, `NAME@A..B`, or `NAME@branch` (its
commits on neither the submodule's checkout nor its repo's default branch). NAME is the submodule, or `owner/repo` to
fetch from that repo. Commits already in the history are dropped. A conflict stops the checkout with the commit and files
named, and puts the repo back where it was. The first cherry-picked PR gets the PR comment when the run has no PR of its
own, and its description goes into `pr.md`.

Build caching: one persistent DerivedData (`TH_DERIVED_DATA`, ~11 GB here) holds the resolved SwiftPM packages, the
module cache and every target's incremental state. What used to defeat it is git: every file git writes gets the current
time as its mtime, and each checkout rewrites files that end up byte-identical (a submodule reset before an override, a
cherry-pick re-applied onto its base), which Xcode and the Swift driver read as changes; the dependency cascade then
recompiled the whole Trio module (about 700 files, 7-8 min on this Mac) even for an unchanged PR. After every checkout the
harness therefore stamps each clean tracked file, in the checkout and all submodules, with an mtime derived from its git
blob hash (`stabilize_mtimes` in `harness/build.py`): identical content always carries the identical timestamp, so only
real changes reach the compiler, and a changed file (a different blob) still gets a different mtime. Files with
uncommitted changes keep their real mtime. `TH_STABLE_MTIMES=0` turns it off; `--clean` still wipes DerivedData.

## Setup

```
./install.sh            # brew deps (libusbmuxd, tesseract, idb-companion), .venv, harness.env, skill symlinks
./bootstrap-xcode.sh    # if Xcode is missing (see below)
./trio-test-harness doctor
```

`harness.env` (gitignored) holds machine-specific settings — Apple team id, VNC password, simulator model. See `harness.env.example`.

### Simulator input backend
`th wda build` compiles appium's WebDriverAgent (cloned into `vendor/`) for the simulator; `th wda start` keeps it running
(`xcodebuild test-without-building`) on port 8100. The simulator driver uses it for taps, typing and the accessibility tree
and falls back to idb when `idb_companion` is installed (Homebrew cannot build idb on this Intel/macOS 15 Mac).

### Xcode
Trio dev needs **Xcode 26.2 or newer** (iOS 26 SDK APIs). On macOS 26 install the current Xcode from the App Store or
`xcodes install --latest`, then `th sim install-runtime`. `bootstrap-xcode.sh` automates the xip/xcodes install, license,
first-launch components and runtime download (it was written for a macOS 15 Intel Mac, where 26.3 was the ceiling).
The harness never needs `xcode-select`; it sets `DEVELOPER_DIR` per command (`TH_XCODE_APP` or newest `/Applications/Xcode*.app`).

See `docs/HANDOFF.md` for the current state and `docs/phone-setup.md` for the iPhone.

### Phone prerequisites
`rooted-vnc`:
- Jailbroken iPhone with **TrollVNC** enabled (Settings ▸ TrollVNC), port 5901. Put `TH_VNC_HOST`, `TH_VNC_PASSWORD`, `TH_DEVICE_PASSCODE` in `.env`.
- `th device vnc-check` grabs a frame; `th device list` shows the USB/usbmuxd view.
- Installing builds: **AppSync Unified** on the phone + `th --target rooted-vnc install --appsync` (pymobiledevice3 over USB);
  or TrollStore (iOS ≤ 17 only) via `--trollstore`; or signed builds (`TH_DEVELOPER_TEAM`, Apple ID in Xcode, Developer Mode) via devicectl.
- Details: `docs/phone-setup.md`.

`iphone-usb` (macOS iPhone Mirroring, built on [phone-harness](https://github.com/ShawnPana/phone-harness), vendored under `vendor/`):
- Mac and iPhone on the **same Apple ID**, Bluetooth + Wi-Fi on, iPhone Mirroring paired once (open the app, confirm on the phone), phone **locked** while mirroring.
- Terminal granted Accessibility + Screen Recording. `th device mirror-status` shows the window; `th --target iphone-usb shot` captures it.
- Installing needs a properly signed build (`TH_DEVELOPER_TEAM`) and Developer Mode on the phone; onboarding is walked (no root to pre-seed).

## CLI overview (`./trio-test-harness --help`; `th` = short alias in `bin/`)
`doctor` · `build` · `install` · `launch` · `sim {list,runtimes,install-runtime,boot,shutdown,erase,container,defaults,openurl}` ·
`device {list,vnc-check,serve-ipa,wake,ssh,diag,vnc-restart,kill,apps}` · `daemon {start,stop,status,restart}` · `wda {build,start,stop,status}` · `shot` · `tap` · `swipe` · `drag` · `scroll` · `type` · `key` · `home` · `tap-text` · `find` ·
`tree` · `wait-text` · `ocr` · `logs [trio|tandem|watch|all]` · `run {new,current,use,note,finish,list,steps}` · `report {html,pr,markdown}` ·
`flow {onboarding-walk,add-cgm-sim,add-pump-sim,back-home,dismiss,at-home}` · `test smoke` · `stress {run,file-issues,scan,faketandem}`

Runs live in `runs/<timestamp>-<name>/` (`screenshots/`, `steps.jsonl`, `run.json`, `report.html`).

`th docs search "<words>" | toc | page <path or URL>[#section]` reads https://triodocs.org (Trio's MkDocs documentation) as
text: search runs over the site's own search index (every section's title and text) and prints links and snippets, `toc`
lists all pages as a tree (from the sitemap), `page` prints a page's article (headings, lists, tables, admonitions,
links) or one section. Pages are cached for a day in `runs/.tmp/triodocs/`. The web UI/CI agent's sandbox allows
`triodocs.org` (and nothing else) for it, and the agents are told to look settings up there before navigating.

`th logs` reads the installed app's log files: Trio's `Documents/logs/log.txt`, TandemKit's
`Documents/tandem/tandem_log.txt` (info and above; debug goes to OSLog only) and the watch log, plus each one's `*_prev`
file for the previous day. It filters with `--grep REGEX [-i]`, `--since 10m|2h|<ISO time>` and `--level warning`
(TandemKit), prints the last `-n` entries (default 200; multi-line entries stay whole, `all` merges by time), lists sizes
with `--files`, and copies the files into the run's `logs/` with `--save`. It reads from the simctl container or over SSH,
or through the target's daemon when one runs, so the sandboxed web UI/CI agent can use it (restart a daemon started
before this command existed). Logs from the phone may hold real device data: keep them out of reports and PRs.

## PR reporting
`./trio-test-harness report pr <PR> [--artifact-url <link>] [--max-shots N]` posts a comment whose screenshots are uploaded
*into* the comment with `gh pr comment --attach` (gh ≥ 2.99; up to 50 files, evenly thinned when a run has more). No extra
repo or hosting. `--mode description` maintains a `<!-- trio-test-harness -->` section in the PR body instead.

## TandemKit stress lane (`th stress`)
`th stress run` builds Trio (nightscout/Trio `feat/tandem-closed-beta`) with its TandemKit submodule at TandemKit `dev`, the
virtual pump (`TANDEMKIT_VIRTUAL_PUMP`) and ThreadSanitizer, runs it on the simulator against faketandem (the Tandem pump
emulator) with a paired pump, closed loop and the Glucose Simulator seeded into the container, and spends the rest of its
time budget on seeded chaos: pump-side events, link faults, app kills, UI noise, each round ending in a check that the
driver recovered. Findings (sanitizer reports, crashes, hangs, no recovery, build breaks) go to `findings.json` and, in
CI, to one issue per fingerprint in this repository (`th stress file-issues`). `.github/workflows/stress-sim.yml` runs it
nightly on the self-hosted workers within a 60-minute job. Details, what is verified and what is not: `docs/stress.md`;
skill `trio-stress`.

## CI (GitHub Actions)
`.github/workflows/e2e.yml` runs `trio-test-e2e --mode simulator` on the self-hosted macOS workers
(home-infra `macos-workers`), triggered by hand from Actions ▸ "Trio e2e (simulator)" ▸ Run workflow, or:

From a laptop, `./trio-test-ci` triggers it with every workflow option as a flag (`./trio-test-ci --help`), prints the
run URL, then follows the run with `gh run watch` (live step progress; `--compact` for failed/relevant steps only) and
prints the job log once the run is done (GitHub publishes job logs only after completion). Ctrl-C stops following without cancelling; `./trio-test-ci --attach <run id>` resumes.

```
./trio-test-ci -s jwoglom/TandemKit#395 --comment
./trio-test-ci 1234 --model opus
./trio-test-ci feat/tandem -s TandemKit@my-branch --instructions "check the bolus sheet"
```

Or with gh directly:

```
gh workflow run e2e.yml -R jwoglom/trio-test-harness -f submodules=jwoglom/TandemKit#395 -f post_comment=true
gh workflow run e2e.yml -R jwoglom/trio-test-harness -f trio=1234
gh workflow run e2e.yml -R jwoglom/trio-test-harness -f trio=feat/tandem -f submodules="TandemKit@my-branch"
```

- **Trio PR or branch**: a PR link, `owner/repo#N`, a nightscout/Trio PR number, or a branch. Blank means `dev`,
  or, with a submodule PR, the Trio branch whose `.gitmodules` has that submodule.
  Branches: `feat/x` (origin, falling back to `upstream`/nightscout/Trio when origin lacks it), `upstream:feat/x` or
  `origin:feat/x` (a remote of the checkout), `owner/repo:feat/x` (any GitHub repo); `@` works in place of `:`.
- **Submodules**: PR links / `owner/repo#N` and `NAME@branch` (or `owner/repo@branch`), space-separated.
- **The run's PR** (read into `pr.md`, and where a comment goes) is the Trio PR, else the first submodule PR.
  `ci/inputs.py` has the mapping onto `trio-test-e2e` flags.
- Also: build configuration, skip onboarding, fake CGM/pump, Claude model (`claude_model`, default `sonnet`), extra
  agent focus, agent time limit, post PR comment, simulator device/runtime, Xcode.
- Every run then has Claude Code exercise what the PR describes (headless, sandboxed; prompt in `ci/agent-prompt.md`,
  live progress in its step log via `ci/agent-stream.py`), and a separate review pass pick the relevant screenshots.

Results: job summary (`ci/summary.py`), an artifact with the run (screenshots, `steps.jsonl`, `report.html`), `report.html`
on its own as an unzipped artifact (it embeds the screenshots), and optionally a PR comment from
`trio-test-harness-runner[bot]` (the App must be installed on the PR's owner; skipped otherwise). GitHub accepts
attachment uploads only from user tokens, so for PRs in jwoglom's repos the screenshots are uploaded with
`TRIO_TEST_HARNESS_USER_PAT` (a fine-grained PAT that can only reach this repo; the images therefore display for people
who can read it) and the App posts the comment. Elsewhere, or if the upload fails, the comment has step captions and a
link to the run instead.
Secrets: `TRIO_E2E_APP_ID`, `TRIO_E2E_APP_KEY` (App with Contents: read,
Pull requests: write) `CLAUDE_CODE_OAUTH_TOKEN`, and `TRIO_TEST_HARNESS_USER_PAT`. In CI the harness authenticates git with `TH_GIT_TOKEN` over
HTTPS instead of SSH, and uses the workers' git mirrors (`TH_GIT_MIRRORS`) and prebuilt WebDriverAgent
(`TH_WDA_PREBUILT`).

## Web UI (the CI job, on this Mac)
`./trio-test-harness webui` serves a page (default `http://127.0.0.1:8787`, open the printed `?token=` URL once) with the
workflow's options plus the target, **rooted-vnc** (the jailbroken iPhone, default) or **simulator**. A run goes through the
same stages as `e2e.yml`, but on this machine with its `harness.env`, `gh` login and `claude` login: `ci/inputs.py` →
`trio-test-e2e` → the sandboxed agent (`ci/agent-prompt.md`, or `ci/agent-prompt-rooted-vnc.md` +
`ci/agent-limits-rooted-vnc.md` for the phone) → the highlights review → `report.html`, shown in the page with the live log
and the agent's findings. Nothing goes to GitHub until you press **Post PR comment** (it runs `th report pr` as you, so the
screenshots are attached inline); ticking "Post the PR comment when done" does it automatically.

- One job at a time; a job first waits until its target is free: `th busy` shows the device lock that `trio-test-e2e`
  holds for its run and a web UI job for its agent step (`harness/busy.py`). The OS frees it when the holder exits, so it
  is never stale, and it is the only signal: process lists are not (an idle `claude ./trio-test-e2e ...` session is not a run).
- The agent gets `TH_RUN=<run dir>`, so its screenshots stay in its run even if another session moves `runs/.current`.
  The harness daemon is started for the agent if none is running, and stopped afterwards only if the web UI started it.
- Builds use `TRIO_REPO` from `harness.env` like any local `trio-test-e2e` run (PR checkouts leave it on a detached HEAD).
- Job state: `runs/.webui/<job>/` (`job.json`, `log.txt`). The server binds to 127.0.0.1 and wants the token (a
  SameSite=Strict cookie) because it can build, drive the phone and comment as you. `--token`/`-p <token>` sets the token
  instead of the random one. `--host 0.0.0.0` (or
  `TH_WEBUI_HOST=0.0.0.0` in `harness.env`; `--port`/`TH_WEBUI_PORT` likewise) makes it reachable from other machines,
  still behind the token; it then prints the LAN URL too. Plain HTTP, so keep it to a network you trust.

**Continue** (a finished job's page, `harness/handoff.py`) carries the run past the sandboxed agent:
- **Continue conversation**: `claude --resume <agent session> --fork-session --remote-control` on this Mac, outside the
  sandbox (the agent's `claude -p` gets a known `--session-id`; older jobs are matched by their prompt). The copy keeps
  everything the agent saw and can edit, commit, push, rebuild and re-test. You drive it at the claude.ai/code link the page
  shows, or in the Claude app. It runs detached in a pseudo-terminal (`harness/ptyrun.py`), so it outlives a web UI restart
  until **Stop** is pressed. The permission mode is picked on the page (auto by default).
- **Make repo copy** (on by default): a git worktree of `TRIO_REPO` at `runs/.worktrees/<run>`, with each submodule a
  worktree of the checkout's own submodule repo at the tested commit (no downloads, about 15 s). Other runs can keep using
  the shared checkout. In the copy (or, unticked, in the shared checkout, only while it is still at the tested commits), Trio
  and every overridden submodule get a local branch named after the tested branch, tracking it on a matching remote (added if
  missing). If that name is taken, the branch is `th/<run>-<n>/<branch>`. **Remove copy** refuses while anything is
  uncommitted and keeps the branches.
- **Start cloud session**: `claude --cloud` from a one-commit checkout of the PR's head branch (the repository under test),
  with the findings and every repo, branch and commit the run built in its first message. It is one repo, gets no transcript
  or screenshots, and cannot build iOS apps.
- **Send findings to that session**: shown when the PR description links the claude.ai/code session that wrote it;
  `claude -p … --cloud <session>` queues the findings there.

State: `runs/.webui/<job>/continue-<n>/` (`meta.json`, `terminal.log`, `system-prompt.md` or `prompt.md`).

Only Trio's simulator CGM/pump and made-up values are used. Reports must not contain real health data, device identifiers,
Apple IDs or tokens. Nothing here is medical advice; Trio builds produced by the harness are for testing only.
