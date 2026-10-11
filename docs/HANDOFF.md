# Handoff: state of the Trio test harness (2026-09-19)

Written for the next agent, who will run on a **newer Mac (macOS 26)**. Everything below was learned on the
first machine (2017 Intel iMac, macOS 15.7) — read "Machine-specific notes" to know what changes.

## Goal (from the user)
Given a Trio branch or PR — or a branch of a Trio submodule such as **TandemKit** (Trio must then be on `feat/tandem`) —
build Trio, install it on the iOS Simulator and/or the user's iPhone, drive the app to test the requested functionality,
save screenshots, and deliver them as artifacts: an HTML page for the user, and PR comments/description when a PR is given.
First validation target: Trio launches and reaches the home screen on **both** targets, with the fake CGM ("Glucose Simulator")
and fake pump ("Pump Simulator") configured.

## Connection modes (2026-09-20)
`simulator`, `rooted-vnc` (jailbroken phone: TrollVNC + SSH + AppSync; verified end to end) and `iphone-usb` (any iPhone via
macOS iPhone Mirroring using phone-harness; driver written and window capture verified, pairing not completed — see
`docs/phone-setup.md`). `--target device` still works as an alias of `rooted-vnc`.

## Entry points (2026-09-20)
- `./trio-test-e2e [--mode simulator|rooted-vnc|iphone-usb] [--pr URL | --branch B] [--trio-branch B] [--cherry-pick SPEC]... [--onboarding preseed|walk] [--skip-build]
  [--no-smoke] [--comment ask|auto|never] [--artifact-url URL] [--agent|--no-agent] [--yes]` — prompts for anything missing on
  a TTY. Submodule PRs are resolved via `.gitmodules` (verified with jwoglom/TandemKit#395 → Trio feat/tandem).
- `./trio-test-harness webui` (2026-09-24): browser form with the CI workflow's options + target (rooted-vnc default, or
  simulator); runs the CI stages locally (`harness/webui.py`) and posts the PR comment only on a button press. See README "Web UI".
- Skills: `trio-test-e2e` (thin wrapper around the script) and `trio-test` (the chain), plus one skill per stage.
- `TRIO_REPO` on this machine is the user's `~/repos/Trio` (their choice); PR/branch checkouts happen there (detached HEAD,
  `git clean -fd` keeps ignored files). Elsewhere the default is `<harness>/Trio`.

## What exists
- `bin/th` CLI + `harness/` package (see README for the command list). Skills in `.claude/skills/`.
- Source selection & build: `harness/build.py` (in-place checkout, branch, PR, fork, submodule overrides, ad-hoc simulator
  signing identical to Trio CI, device signing or ad-hoc IPA). Git calls rewrite `https://github.com/` → SSH and never prompt
  (needed for the private `jwoglom/tandemkit` submodule).
- Simulator driver `harness/sim.py`: simctl for boot/install/launch/screenshot; **WebDriverAgent** (`harness/wda.py`,
  `th wda build|start|stop|status`, cloned into `vendor/`) for taps, typing, alerts and the accessibility tree; idb as an
  alternative if `idb_companion` exists.
- Phone driver `harness/vnc.py` + `harness/rfb.py`: TrollVNC over Wi-Fi or USB (`iproxy`). OCR fallback `harness/ocr.py`.
- Reporting `harness/report.py`: runs under `runs/`, `report.html` (self-contained), PR comment/description via `gh` with
  screenshots attached inline with `gh pr comment --attach` (gh ≥ 2.99, installed: 2.100).
- Trio flows `harness/flows/trio.py`: onboarding walk, pre-seeding the onboarding flag, add CGM/pump simulator, back-to-home,
  `th test smoke` composing them.
- `install.sh` (deps, venv, skills), `bootstrap-xcode.sh` (Xcode install helper), `harness.env.example`.

## Verified end to end on 2026-09-20 (both targets)
- **Simulator** (iPhone 17 / iOS 26.3, Xcode 26.3): build → install → launch with pre-seeded onboarding → home →
  Glucose Simulator + Pump Simulator via the home shortcuts, driven through WebDriverAgent. Report artifact published.
- **Phone** (iOS 18.6.1, rootless jailbreak): unsigned build + codesign fake-sign (every dylib incl. Trio.debug.dylib) →
  `th --target rooted-vnc install --appsync` (24 s) → `uiopen` over SSH → full onboarding via `th flow onboarding-walk`
  (OCR over VNC; the two Bluetooth alerts and one Next were tapped by hand while the OCR was being fixed) → home →
  Glucose Simulator + Pump Simulator. Crash fixes on the way: Trio.debug.dylib must be signed; Crashlytics dies with
  EXC_GUARD on the jailbroken phone, so `crashlyticsSharingEnabled=false` is pre-seeded by `th launch --preseed-onboarding`.
  Phone unlock works with `th device unlock` (keypad taps, one fast sequence — the phone re-locks within seconds).

## Verified (actually ran)
- Checkout of `feat/tandem` incl. TandemKit submodule; `--submodule TandemKit@main` override; `th build --checkout-only`.
- Simulator: create/boot (`TrioHarness`), screenshots, WDA build + start, coordinate tap, `tap-text`, `find` (fast predicate
  search with correct pixel rects), `home`.
- Phone: RFB connect + VNC auth, screenshot, Home press. `th device list` via pymobiledevice3.
- OCR: phrase search incl. white-on-blue button text on synthetic iPhone-size images; run/report HTML + markdown rendering.
- Trio submodules initialized in the user's `~/repos/Trio` (dev branch).
- **Phone, 2026-09-20 (PR jwoglom/Trio#6)**: full exploratory pass over Settings ▸ Notifications ▸ Live Activity: pickers,
  menus, help sheets, the new drag-and-drop editor (`th drag` = long-press + move: reorder, drop onto the palette to remove,
  drag up from the palette to add — all worked over TrollVNC), tap-to-add, trash buttons, persistence across re-entry, and
  the real Lock Screen Live Activity (`th device lock` twice = lock, then wake to the Lock Screen; `th device unlock` after).
  Report artifact with 35 screenshots published.

## Still unverified
- `th report pr` against a real PR (gh --attach inline uploads; ask before posting).
- `th webui` with a real job: only tested with the stages stubbed (auth, validation, stage order, sandbox settings per
  target, comment gating). Unverified: the sandboxed agent on rooted-vnc (daemon socket `daemon-rooted-vnc.sock`, `device
  lock/unlock` through the daemon from inside the sandbox) and a real `report pr` from the button.
- `th webui` live phone panel (`harness/liveview.py`, noVNC 1.7.0 from jsdelivr): verified 2026-09-24 showing the real
  phone through TrollVNC in the user's browser (TrollVNC accepts noVNC's encodings and extended clipboard). Not yet
  checked: control input through noVNC, the panel's buttons, and the USB fallback when Wi-Fi refuses.
  After a phone reboot the rootless jailbreak must be re-run before TrollVNC/SSH answer.
- `th test smoke` as a single command (the pieces were run individually).
- Onboarding walk fully unattended on the phone: the iOS alert "Allow" and the "Next" under a dimmed alert are now handled
  (isolated-word OCR, banded passes, alerts checked before Next) but that exact combination has not been re-run end to end.

## Loop (`--app loop`, added 2026-10-11): nothing has run on a Mac yet
Written from Loop's source (LoopKit/Loop dev, LoopOnboarding, LoopKit's MockKit) and checked offline only: argument
parsing, the generated `build/override-loop-*.xcconfig`, `prepare_source` on a LoopWorkspace clone, PR resolution (a
TandemKit or LoopKit/Loop PR finds LoopKit/LoopWorkspace `feat/tandem-closed-beta`, also from a fork's checkout), `th docs`
against LoopDocs, and `ci/inputs.py`. To verify, in order, on the simulator first (`./trio-test-e2e --app loop --mode
simulator --branch feat/tandem-closed-beta`):
- The build: `LoopWorkspace` scheme with `-xcconfig build/override-loop-sim.xcconfig` and ad hoc signing; that the app is
  `com.TRIOSIMTST.loopkit.Loop` with the app group `group.com.TRIOSIMTST.loopkit.LoopGroup` (`th sim container`), and that
  Loop's watch app and plugins build under the harness's Xcode (LoopWorkspace's own CI uses a newer one).
- Pre-seeding: that after `reset_after_install` and `preseed_simulator` Loop opens past onboarding with the mock therapy
  settings (Settings ▸ Therapy Settings), i.e. that restarting the simulator's `cfprefsd` (children of its `launchd_sim`)
  makes Loop read the plist written into the app group.
- The prompts after onboarding (notifications, Apple Health's "Turn On All" + "Allow", Bluetooth): whether WebDriverAgent's
  tree shows them, and whether `at_home`'s blocker check sees them over the home screen.
- `walk_onboarding` from a fresh install (`--onboarding walk`): the logo's position between the title and the text, and
  that a 3 s press skips onboarding.
- Adding the CGM Simulator and Pump Simulator from the HUD (and through Settings on a retry), and `give_cgm_glucose`
  (the sine-curve model written into `Documents/CGMManagerState.plist`, then a relaunch).
- The phone (`--mode rooted-vnc`): fake-signing with Loop's entitlements (`$(APP_GROUP_IDENTIFIER)`, HealthKit, Siri,
  NFC, time-sensitive notifications) under AppSync, the OCR-only home detection (chart titles; the toolbar is icons) and
  the HUD's "Add CGM" / "Add Pump" found by OCR.
- CI (`./trio-test-ci --app loop`): a LoopWorkspace clone and its submodules on the workers (no LoopWorkspace mirror is
  assumed), and the agent with `ci/agent-prompt-loop.md`.

## Earlier checklist (kept for reference; most items are now done)
0. **Install both simulator runtimes**: `th sim install-runtime` (iOS) **and** `th sim install-runtime --platform watchOS`.
   Verified on 2026-09-19: with the harness flags, `xcodebuild -resolvePackageDependencies` for Trio succeeds, and the build
   then aborts immediately with "This scheme builds an embedded Apple Watch app. watchOS 11.0 must be installed" when the
   watchOS runtime is missing. `th doctor` now checks for it.
1. **A real Trio build** (`th --target sim build`). The first Mac only had Xcode 16 (iOS 18 SDK); Trio dev uses iOS 26 APIs
   (`.glassEffect` in `Trio/Sources/Modules/Home/View/GlassChrome.swift` and `Views/GlassActionSheet.swift`, AlarmKit behind
   `canImport`), so it needs Xcode 26.x. Trio CI uses Xcode 26.2/26.5; anything 26.2+ should do. Check the xcodebuild flags in
   `harness/build.py` against `.github/workflows/unit_tests.yml` in Trio if the build fails for signing reasons.
2. `th install` + `th launch --preseed-onboarding` on the simulator, then `th flow at-home`. The pre-seed writes
   `Documents/onboardingCompleted.plist` (binary plist, root Bool true) into the app container — confirm Trio honours it.
3. The onboarding walk (`th flow onboarding-walk`) and the fake-device flows (`th flow add-cgm-sim`, `add-pump-sim`) — written
   from code reading, never run. Expect to adjust labels/timings. Facts they rely on are listed at the top of
   `harness/flows/trio.py` and in `.claude/skills/trio-ui-drive/SKILL.md`. Watch for: Bluetooth "required" view on the CGM
   screen in the simulator, iOS notification permission alert, version-check alert, the "chapter complete" interstitials.
4. `th test smoke` end to end, then `th report html` → publish as an Artifact.
5. Phone: AppSync Unified is installed (2026-09-20, see `docs/phone-setup.md`). Next: `th --target rooted-vnc build --trollstore`
   (produces the ad-hoc IPA), `th --target rooted-vnc install --appsync`, `th launch --target rooted-vnc`, walk onboarding via OCR.
6. `th report pr` against a real PR (uses `gh --attach`; ask the user before posting).

## Machine-specific notes (what to redo on the new Mac)
- Run `./install.sh` (brew: libusbmuxd, tesseract, coreutils, idb-companion if it builds; venv; skills symlinks), then
  copy `harness.env.example` → `harness.env` and recreate `.env` with `TH_VNC_HOST` (phone IP from Settings ▸ TrollVNC) and
  `TH_VNC_PASSWORD`. These are not in git.
- Xcode: on macOS 26 just install the current Xcode (App Store or `xcodes install --latest`); `bootstrap-xcode.sh`'s
  Intel/macOS-15 constraints no longer apply. The harness never needs `xcode-select`; it sets `DEVELOPER_DIR` from
  `TH_XCODE_APP` or the newest `/Applications/Xcode*.app`.
- iOS simulator runtime: `th sim install-runtime` (or Xcode ▸ Settings ▸ Components). Set `TH_SIM_DEVICE` to a model the
  runtime has (default "iPhone 16"; Trio CI uses "iPhone 17"). Trio deployment target is iOS 17.
- `th wda build` once per Xcode/runtime; `th wda start` after each simulator boot. If WDA fails with "Timed out waiting for AX
  loaded notification", run `xcrun simctl spawn <udid> defaults write com.apple.Accessibility AccessibilityEnabled -bool true`
  (and `ApplicationAccessibilityEnabled`), reboot the simulator, retry. Startup can take minutes on slow Macs.
- On Apple silicon `brew install facebook/fb/idb-companion` should work (it needs Xcode 26); the harness then also accepts idb.
- The phone must be on the same network (or USB) as the new Mac. TrollVNC only allows what the RFB client asks for; our
  client requests raw encoding, 32bpp.
- GitHub: `gh auth login` as jwoglom (repo scope). Trio origin is `git@github.com:jwoglom/Trio`; upstream `nightscout/Trio`.

## Known quirks
- **Load**: the iOS 26 simulator on this Intel iMac is heavy (Metal shader compilers, indexing). Booting it while xcodebuild
  runs pushed the load average past 300 and stalled everything, including the phone connection. Shut the simulator down
  during builds; boot it only to install/test. Never `pkill -f harness.cli` broadly — it kills a running `th build` too.
- Phone cannot reach the Mac's subnet; use `ssh -R` tunnels for phone→Mac transfers (details in phone-setup.md).
- vncdotool (Twisted) hangs against TrollVNC after auth → we use `harness/rfb.py`. Keep it.
- WDA `/source` dumps of SpringBoard report zero frames for icons; the predicate `find` path returns correct rects. Inside apps
  both work. `/source` takes ~10 s; prefer `th find`/`th tap-text`.
- `/tmp` writes were blocked in the agent sandbox; scratch files go to `runs/.tmp/` (`util.tmp_path`).
- OCR on the whole 1179×2556 screenshot takes 1–5 s per pass; passes stop at the first hit.
- The harness never writes into the Trio checkout for signing: settings (placeholder team `TRIOSIMTST` for the simulator,
  `DEV_BRANCH_WARNING_DISABLED` like CI) live in `build/override-<target>.xcconfig` and reach xcodebuild via `-xcconfig`.
  A user's own `ConfigOverride.xcconfig` is read for its team and otherwise untouched (older harness versions wrote a marked
  file into the checkout; it is removed automatically when found).
- The `--trollstore` install path exists but is irrelevant for an iOS 18 phone; the IPA it builds is what `--appsync` installs.
- **Two Claude Code sessions on one phone collide.** A second session's `trio-test-e2e` relaunched Trio mid-test and, because
  `runs/.current` is a single shared pointer, redirected the other session's screenshots into its own run. Before driving the
  phone, run `./trio-test-harness busy` and wait while it names a holder (the device lock; process lists mislead:
  an idle `claude ./trio-test-e2e ...` session is not a run); always pass `--run <dir>` to `th shot`/`run note`/`run finish` when two runs
  may exist. `Run` paths are now made absolute and step screenshots are stored run-relative (a relative `--run runs/x` used
  to log `runs/x/screenshots/…`, which the report could not find and drew as black cards).
- **Every build recompiled all of Trio** (2026-09-30 analysis of the web UI logs: 702/702 Trio files even for a byte-identical
  re-run, 7-8 min of xcodebuild on this 2-core Intel MacBook Pro). Cause: git stamps every file it writes with the current
  time, and the checkout stage rewrites unchanged files (submodule reset then override, cherry-pick re-applied); Xcode and
  the Swift driver treat the new mtime as a change and the cascade takes the whole app module with it. Fix: after the
  checkout, `stabilize_mtimes` gives every clean tracked file an mtime derived from its git blob hash (`TH_STABLE_MTIMES`,
  README "Build caching"). Not done, and probably not worth it on this disk: a DerivedData per Trio base branch, or Xcode's
  content-addressed compilation cache (`COMPILATION_CACHE_ENABLE_CACHING=YES`); the disk was at 100% during the analysis.
- SwiftUI drag & drop needs a long press before the move: use `th drag`, not `th swipe`. On the simulator `drag` maps to
  WDA's `dragfromtoforduration` (its duration is the press-hold time).

## Open questions for the user
- Whether AppSync Unified is acceptable on the phone (user said yes on 2026-09-19) and which package manager is installed.
- For PRs: comment by default; only edit the description when asked.
