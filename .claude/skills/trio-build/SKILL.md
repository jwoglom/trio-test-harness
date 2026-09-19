---
name: trio-build
description: Build and install the Trio iOS app from any branch, PR, or submodule branch (e.g. TandemKit) for the iOS Simulator or the USB iPhone, using the trio-test-harness `th build` / `th install` commands. Use when asked to build Trio, install Trio on the simulator/phone, or when a Trio test needs a fresh build.
---

# trio-build

`cd ~/repos/trio-test-harness`; `th` = `./bin/th`. Requires Xcode (see `th doctor`; if missing, the user runs `./bootstrap-xcode.sh`).

## Source selection (`th build`)
| Want | Flags |
|---|---|
| The user's checkout as-is (`~/repos/Trio`, uncommitted changes included) | *(no flags)* or `--in-place ~/repos/Trio` |
| A branch of Trio's origin (jwoglom/Trio) | `--branch feat/tandem` |
| A branch of another fork | `--branch nightscout/Trio@dev` or `--branch https://github.com/x/Trio#branch` |
| A pull request | `--pr 4321` (nightscout/Trio), `--pr jwoglom/Trio#12`, or `--pr <URL>` |
| A submodule at a branch/PR/fork | add `--submodule TandemKit@feat/x`, `--submodule TandemKit@pr/12`, `--submodule TandemKit=https://github.com/fork/TandemKit#br` (repeatable) |
Branch/PR builds use a disposable clone at `build/Trio-src` (never the user's checkout). Submodules are synced with
`git submodule update --init --recursive` — the first time this downloads LoopKit & friends (minutes). `--no-fetch` skips it.

## Targets
- `--target sim` (default): ad-hoc signed like Trio CI — no Apple credentials. `th install` puts it on the `TrioHarness`
  simulator (created on demand from `TH_SIM_DEVICE`/`TH_SIM_RUNTIME`).
- `--target device` **signed**: needs `TH_DEVELOPER_TEAM` in `harness.env` and an Apple ID signed into Xcode (or ASC API
  key). Installs with `xcrun devicectl`. Requires Developer Mode on the phone.
- `--target device --trollstore`: ad-hoc signed `.ipa` (no Apple credentials). Install it with
  `th --target device install --appsync` (phone has AppSync Unified; uses pymobiledevice3 over USB — the user's iOS 18
  phone), or `--trollstore --open-on-device` on an iOS ≤ 17 TrollStore phone (serves the IPA over HTTP and types the
  `apple-magnifier://install?url=…` link into Safari via VNC). See `docs/phone-setup.md`.

## Notes
- Build output: `build/last-build.json` (app path, bundle id, git sha, submodule shas) — `th run new` copies it into the run.
- `CI=true` is exported so Trio's SwiftFormat and Crashlytics build phases are skipped; `DEV_BRANCH_WARNING_DISABLED` is set so
  the dev-branch popup does not block automation (same as Trio CI). Derived data: `~/Library/Developer/Xcode/DerivedData/trio-test-harness`.
- Errors: `th build` prints `error:` lines; full log at `build/xcodebuild-<target>.log`. A failing build of the change under test is a test result — report it.
- `--clean` wipes derived data (slow rebuild). `--configuration Release` if the user wants release behaviour.
- Watch app / widgets are built with the scheme; nothing extra needed.
