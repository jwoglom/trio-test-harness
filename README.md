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
`tree` · `wait-text` · `ocr` · `run {new,current,use,note,finish,list,steps}` · `report {html,pr,markdown}` ·
`flow {onboarding-walk,add-cgm-sim,add-pump-sim,back-home,dismiss,at-home}` · `test smoke`

Runs live in `runs/<timestamp>-<name>/` (`screenshots/`, `steps.jsonl`, `run.json`, `report.html`).

## PR reporting
`./trio-test-harness report pr <PR> [--artifact-url <link>] [--max-shots N]` posts a comment whose screenshots are uploaded
*into* the comment with `gh pr comment --attach` (gh ≥ 2.99; up to 50 files, evenly thinned when a run has more). No extra
repo or hosting. `--mode description` maintains a `<!-- trio-test-harness -->` section in the PR body instead.

## CI (GitHub Actions)
`.github/workflows/e2e.yml` runs `trio-test-e2e --mode simulator` on the self-hosted macOS workers
(home-infra `macos-workers`), triggered by hand from Actions ▸ "Trio e2e (simulator)" ▸ Run workflow, or:

From a laptop, `./trio-test-ci` triggers it with every workflow option as a flag (`./trio-test-ci --help`), prints the
run URL, then follows the run: steps as they start and finish, and the job log once the job is done (GitHub publishes job
logs only after completion). Ctrl-C stops following without cancelling; `./trio-test-ci --attach <run id>` resumes.

```
./trio-test-ci -s jwoglom/TandemKit#395 --comment
./trio-test-ci 1234 --agent none
./trio-test-ci feat/tandem -s TandemKit@my-branch --instructions "check the bolus sheet"
```

Or with gh directly:

```
gh workflow run e2e.yml -R jwoglom/trio-test-harness -f submodules=jwoglom/TandemKit#395 -f post_comment=true
gh workflow run e2e.yml -R jwoglom/trio-test-harness -f trio=1234 -f agent=none
gh workflow run e2e.yml -R jwoglom/trio-test-harness -f trio=feat/tandem -f submodules="TandemKit@my-branch"
```

- **Trio PR or branch**: a PR link, `owner/repo#N`, a nightscout/Trio PR number, or a branch. Blank means `dev`,
  or, with a submodule PR, the Trio branch whose `.gitmodules` has that submodule.
- **Submodules**: PR links / `owner/repo#N` and `NAME@branch` (or `owner/repo@branch`), space-separated.
- **The run's PR** (read into `pr.md`, and where a comment goes) is the Trio PR, else the first submodule PR.
  `ci/inputs.py` has the mapping onto `trio-test-e2e` flags.
- Also: build configuration, skip onboarding, fake CGM/pump, `agent` (Claude Code then exercises what the PR
  describes, headless; prompt in `ci/agent-prompt.md`), extra agent focus, agent time limit, post PR comment,
  simulator device/runtime, Xcode.

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

## Safety
Only Trio's simulator CGM/pump and made-up values are used. Reports must not contain real health data, device identifiers,
Apple IDs or tokens. Nothing here is medical advice; Trio builds produced by the harness are for testing only.
