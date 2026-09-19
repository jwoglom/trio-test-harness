# trio-test-harness

Tooling + Claude Code skills to **build, install, drive and screenshot the Trio iOS app** on either
the iOS Simulator or a USB-attached iPhone, from any Trio branch / PR / submodule branch, and to
turn the screenshots into an HTML report (claude.ai Artifact) or a GitHub PR comment.

```
th build --branch feat/tandem --submodule TandemKit@my-change      # or --pr <url>
th install --reinstall
th launch --preseed-onboarding
th shot --name home --note "home after launch"
th tap-text Settings ; th tap-text Devices ; th shot --name devices
th run finish passed --summary "..." ; th report html ; th report pr <pr-url>
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

Python package `harness/` (CLI `bin/th`), Claude skills in `.claude/skills/` (symlinked into `~/.claude/skills` by `install.sh`):

- `trio-test` — the orchestration skill (parse request → build → drive → report)
- `trio-build`, `trio-ui-drive`, `trio-report` — the supporting skills

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
`device {list,vnc-check,serve-ipa,wake}` · `shot` · `tap` · `swipe` · `scroll` · `type` · `key` · `home` · `tap-text` · `find` ·
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
