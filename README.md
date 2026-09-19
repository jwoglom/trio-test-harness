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
| Build | `xcodebuild`, ad-hoc signing (no Apple credentials), same flags as Trio CI | ad-hoc `.ipa` for **TrollStore**, or automatic signing with `TH_DEVELOPER_TEAM` + `devicectl` |
| Install / launch | `simctl install` / `simctl launch` | TrollStore via `apple-magnifier://install?url=…` (harness serves the IPA), launch via Spotlight over VNC; or `devicectl` |
| Screenshots | `simctl io screenshot` | **TrollVNC** framebuffer over USB (`iproxy 5901`) |
| Taps / swipes / typing | **idb** (`idb ui tap/swipe/text`) | VNC pointer & keyboard events (TrollVNC maps right-click → Home, middle → Power) |
| Find elements | idb accessibility tree (`th tree`, `th tap-text`) | OCR (tesseract) on the screenshot (`th tap-text`, `th find`) |
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

### Xcode on this Mac
The harness targets the 2017 Intel iMac running macOS 15.7. The newest Xcode that runs there is **26.3** (26.4+ needs macOS 26).
Apple gates Xcode downloads behind an Apple ID, so `bootstrap-xcode.sh` either expands an `Xcode_26.3_Universal.xip` you
drop into `~/Downloads`, or uses `xcodes` (interactive Apple ID login). It then accepts the license / first-launch
components (sudo), downloads the iOS simulator runtime, and installs `idb-companion`. Budget ~30 GB of disk.
The harness never needs `xcode-select`; it sets `DEVELOPER_DIR` per command.

### Phone prerequisites
- TrollStore + **TrollVNC** installed and enabled (Settings ▸ TrollVNC). Default port 5901; set `TH_VNC_PASSWORD` if you set one.
- Plugged in over USB, unlocked, trusted. `th device list` shows it; `th device vnc-check` grabs a frame.
- For TrollStore installs the phone must reach the Mac over the LAN (or set `TH_IPA_HTTP_HOST`, e.g. a Tailscale IP).
- For signed installs instead: `TH_DEVELOPER_TEAM`, Apple ID signed into Xcode, Developer Mode on the phone.

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
