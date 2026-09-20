# trio-test-harness

Tooling + Claude Code skills to **build, install, drive and screenshot the Trio iOS app** on either
the iOS Simulator or a USB-attached iPhone, from any Trio branch / PR / submodule branch, and to
turn the screenshots into an HTML report (claude.ai Artifact) or a GitHub PR comment.

```
bin/trio-test --mode simulator --pr https://github.com/nightscout/Trio/pull/1234     # Trio PR
bin/trio-test --mode vnc --pr https://github.com/jwoglom/TandemKit/pull/395          # submodule PR (Trio @ feat/tandem)
bin/trio-test                                                                        # prompts for mode and PR/branch
```
`trio-test` runs checkout → build → install → launch (onboarding pre-seeded) → home screen → Glucose Simulator +
Pump Simulator → `report.html`, writes `runs/<run>/pr.md` with the PR description and a test-plan stub, then hands off to
Claude Code (from a terminal) or to the `trio-e2e` skill (inside Claude) for the PR-specific testing, the report artifact
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

| Piece | Simulator | Physical iPhone |
|---|---|---|
| Build | `xcodebuild`, ad-hoc signing (no Apple credentials), same flags as Trio CI | ad-hoc `.ipa` installed via **AppSync Unified** (or TrollStore on iOS ≤ 17), or automatic signing with `TH_DEVELOPER_TEAM` + `devicectl` |
| Install / launch | `simctl install` / `simctl launch` | `pymobiledevice3 apps install` (AppSync), or TrollStore link, or `devicectl`; launch via Spotlight over VNC |
| Screenshots | `simctl io screenshot` | **TrollVNC** framebuffer over USB (`iproxy 5901`) |
| Taps / swipes / typing | **WebDriverAgent** (`th wda start`; idb if installed) | VNC pointer & keyboard events (TrollVNC maps right-click → Home, middle → Power) |
| Find elements | WebDriverAgent accessibility tree (`th tree`, `th tap-text`) | OCR (tesseract) on the screenshot (`th tap-text`, `th find`) |
| Skip onboarding | write `Documents/onboardingCompleted.plist` into the app container | click through with `th flow onboarding-walk` |

All coordinates the CLI accepts are **screenshot pixels**, so an agent can look at a PNG and tap what it sees on both targets.

Python package `harness/` (CLIs `bin/trio-test`, `bin/th`), Claude skills in `.claude/skills/` (symlinked into `~/.claude/skills` by `install.sh`):

- `trio-e2e` — runs `bin/trio-test`, then the PR-specific testing + report + comment prompt
- `trio-test` — the chained workflow, stage by stage, pointing at the per-stage skills
- per stage: `trio-sim-setup`, `trio-phone-setup`, `trio-checkout`, `trio-build`, `trio-install`, `trio-launch`,
  `trio-fake-devices`, `trio-pr-plan`, `trio-ui-drive`, `trio-report`

Source selection: `TRIO_REPO` is the checkout the harness uses (this machine: `~/repos/Trio`; default elsewhere:
`<harness>/Trio`, cloned on demand with `TH_TRIO_REFERENCE` as a git reference). A PR on a submodule repo is detected
through `.gitmodules` of the candidate Trio branches (`--trio-branch`, `TH_TRIO_BASE_BRANCH`, dev, feat/tandem, main).

## Setup

```
./install.sh            # brew deps (libusbmuxd, tesseract, idb-companion), .venv, harness.env, skill symlinks
./bootstrap-xcode.sh    # if Xcode is missing (see below)
./bin/th doctor
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
- Jailbroken iPhone with **TrollVNC** enabled (Settings ▸ TrollVNC), port 5901. Put `TH_VNC_HOST` and `TH_VNC_PASSWORD` in `.env`.
- `th device vnc-check` grabs a frame; `th device list` shows the USB/usbmuxd view.
- Installing builds: **AppSync Unified** on the phone + `th --target device install --appsync` (pymobiledevice3 over USB);
  or TrollStore (iOS ≤ 17 only) via `--trollstore`; or signed builds (`TH_DEVELOPER_TEAM`, Apple ID in Xcode, Developer Mode) via devicectl.
- Details: `docs/phone-setup.md`.

## CLI overview (`th --help`)
`doctor` · `build` · `install` · `launch` · `sim {list,runtimes,install-runtime,boot,shutdown,erase,container,defaults,openurl}` ·
`device {list,vnc-check,serve-ipa,wake,ssh,diag,vnc-restart,kill,apps}` · `daemon {start,stop,status,restart}` · `wda {build,start,stop,status}` · `shot` · `tap` · `swipe` · `scroll` · `type` · `key` · `home` · `tap-text` · `find` ·
`tree` · `wait-text` · `ocr` · `run {new,current,use,note,finish,list,steps}` · `report {html,pr,markdown}` ·
`flow {onboarding-walk,add-cgm-sim,add-pump-sim,back-home,dismiss,at-home}` · `test smoke`

Runs live in `runs/<timestamp>-<name>/` (`screenshots/`, `steps.jsonl`, `run.json`, `report.html`).

## PR reporting
GitHub's API cannot attach images to comments, so `th report pr` uploads the run's screenshots to the public repo
`TH_ARTIFACT_REPO` (default `jwoglom/trio-test-screenshots`, created on first use) and posts a comment with a screenshot
table. `--mode description` maintains a `<!-- trio-test-harness -->` section in the PR body instead.

## Safety
Only Trio's simulator CGM/pump and made-up values are used. Reports must not contain real health data, device identifiers,
Apple IDs or tokens. Nothing here is medical advice; Trio builds produced by the harness are for testing only.
