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
- **`th db`, 2026-10-17 (Trio on the simulator)**: `stores`/`tables`/`schema`/`query` (read-only: an UPDATE through
  `query` is refused) and `exec` (terminate → backup into the run's `db/` → transaction → run step → relaunch; a failing
  statement changes nothing and still relaunches). Under the agent's sandbox (`claude -p` with `ci/claude-sandbox.py`'s
  settings): without the app's containers `th db` sees no store (the CI agent's "outside the readable/writable paths"
  on Trio PR 1580); with them it works, and another app's container (Safari) stays unreadable.
  `blockReadsOutsideWorkingDirectories` also removes sandboxed commands' reads of /Users, so the containers need
  `allowRead` as well as `allowWrite`. Loop's store (`Model.sqlite` in the app group) is not verified yet.
- **`th oslog` / `crashes` / `files` / `perf`, 2026-10-17 (Trio on the simulator, directly and from the agent's
  sandbox through the daemon)**: `oslog --since` (`log show`, ~20 s here, minutes while the Mac is busy) shows Trio's
  debug lines once `--enable-debug` set the subsystem to persist debug; `--stream N` records them live (log's own
  `--timeout` ends it: killing xcrun left the in-simulator `log stream` running). `crashes` found a report of a
  SIGSEGV/SIGABRT sent to the app (macOS wrote it a couple of minutes later, and an external SIGABRT alone wrote none),
  `--save` stored it without the simulator's UUID or home paths and logged a step, which report.html shows; the pipeline's
  `collect_crashes` did the same. `files ls|cat` (plists as JSON, `..` refused); `perf` and `--watch --save` (perf.csv).
  `perf --leaks`: the simulator build is "not debuggable" (no get-task-allow), so leaks sees only read-only memory and
  takes minutes on this Mac. **Unverified: every phone (rooted-vnc) path** — the phone was not reachable (not on USB,
  SSH unreachable): `syslog collect` + `log show` on the archive, `crash pull`, `files` over SSH, `ps` over SSH.

- **Apple Watch (`--target watch`), 2026-10-18 (Trio dev + nightscout/Trio#1648 on this 4-core Intel MacBook, Xcode
  26.3, iOS 26.3 + watchOS 26.2 simulators)**: `TrioHarnessWatch` (Series 11 46 mm) paired with `TrioHarness`; the
  build's embedded Watch app installed straight onto the watch (installing the phone app does not install it there); the
  watch agent (`harness/watchagent`, a watchOS XCUITest runner) built in ~20 s and ran beside WebDriverAgent, each on its
  own simulator. Real WatchConnectivity between the two: the phone's glucose and IOB reached the watch, and a Watch bolus
  reached the phone. Through the agent: `tree` (full SwiftUI tree with labels), `tap`/`tap-text`, `crown` (0.05 turn =
  one 5% step of the confirmation bar; a quarter turn dialled the bolus input to 1.40 U), `home`, all also through
  `th --target watch daemon`. PR #1648's override ran end to end with max IOB 0 U: bolus 0.10 U, crown confirm, the
  phone's "Bolus blocked: … would exceed max IOB" shown on the Watch, crown again, phone log `User confirmed bolus from
  watch past safety checks: ["max_iob"]` and `Enacted bolus via APS Manager: 0.1U`.
  `trio-test-e2e --mode simulator --in-place ~/repos/Trio --skip-build --watch` ran clean from a fresh install: Watch
  app installed, launched before the phone app, `watch-home` and `watch-with-data` steps (glucose and IOB on the watch).
  Learned: launch the Watch app before the phone app (the phone session logged isReachable false until Trio was
  relaunched); a List row's button takes taps only on its icon or text, so `WatchDriver.tap_text` aims at those; the
  Watch app's own log file is not on the watch's disk until it flushes, and reaches the phone's `th logs watch` then.
  **Watch settings, 2026-10-18**: wrist and crown side are `com.apple.nano` `wornOnRightArm` / `invertUI` (each on its
  own: wrist right when the first is true, crown left when the second is), text size UIKit's
  `UIPreferredContentSizeCategoryName` (`simctl ui content_size` refuses a watchOS runtime). Read back through the agent's
  `device` call (WatchKit's `wristLocation`, `crownOrientation`, `screenBounds`): apps read them at launch, a running
  process keeps the old values, so `th watch settings` relaunches the Watch app; the simulator never restarts. Seen in
  Trio: wrist right turns the bolus confirmation's crown arrow counterclockwise; crown left moves the page dots left;
  the largest accessibility size grows "Bolus" from 87×41 to 111×55 px.
  **Device scope classifier, 2026-10-18**: Haiku through `claude -p` (~10 s) answered phone+watch for #1648 and phone for
  #1642 (Glycemia Risk Index), #1644 (Live Activity colour) and jwoglom/TandemKit#601, each with a sensible reason.
  **Watch pairing: one phone per watch model (2026-10-19).** A phone simulator that has been paired again (a second
  watch, `pair_activate`, `unpair`) or erased stops linking Watch apps to their iPhone apps: its wcd still sees the
  Watch app on the watch (`enumerateLocallyAvailableApplications…`) but logs `iOSApplicationsWithWatchAppInstalled:
  {()}`, so Trio's `WCSession.isWatchAppInstalled` is false and it never sends the watch anything (`Skipping
  setupWatchState`), while simctl shows the pair active and connected and the watch side reports the phone reachable.
  Nothing short of a new phone simulator brought it back: restarting the phone's wcd/nanoregistryd/IDS, rebooting
  either side, removing the other pair, erasing the phone, then both, then pairing afresh (the 40 mm and then the
  46 mm watch), waiting for pairedsyncd's initial sync (Complete) and an hour of relaunches. A never-used phone and
  watch, paired while shut down, phone booted first, Trio installed on the phone and then its Watch app on the watch,
  linked on the first launch (`iOSApplicationsWithWatchAppInstalled: {( "org.nightscout..trio" )}`, `isReachable:
  true`). So `ensure_pair` gives each watch model its own phone, `TrioHarness (<model>)` with `TrioHarnessWatch
  (<model>)`, paired once at creation, and makes it the phone every command drives (`runs/.tmp/sim-udid.txt`, read by
  `xcode.ensure_simulator`, `SimulatorDriver.udid` and WebDriverAgent, which moves with it). `th sim erase` refuses a
  paired phone; `th watch pair --recreate` makes both anew. Switching model is switching phone, so a model's Trio has
  its own data. The phone app goes on before its Watch app everywhere (pipeline, `th watch pair`). The model is chosen
  when a watch starts (`--watch-model`, `th watch pair --model`), which shuts the pair in use down; `boot_simulator`
  shuts down any other booted harness iPhone or watch, so one of each runs. Defaults: `--devices phone` in the CLI, CI
  and the web UI; auto classifies every PR of the run (app, submodule, other submodule PRs, cherry-picks), for Loop too,
  where a phone+watch answer runs phone-only.
  Two more on a paired phone: uninstalling the phone app also deletes its Watch app, and that deletion reached the watch
  ~14 s later, 3 s after the pipeline had reinstalled the Watch app, which then vanished; the pipeline now uninstalls the
  Watch app first, and `WatchDriver.install` checks ~10 s later and installs again. And watchOS returns to the clock
  after 2 minutes without a touch, so a later screenshot showed the watch face: the driver brings the launched Watch app
  back before each screenshot, tap, crown turn or tree read, unless `home` put the clock there. Verified 2026-10-19 on
  the new pair: `trio-test-e2e --watch` clean (fresh reinstall, CGM + pump, `watch-with-data` with the phone's glucose)
  and #1648's override flow through the CLI.
  Also found: `XCUIApplication.launch()` failing on a just-erased watch fails the agent's test and so ends the agent;
  the harness now launches with simctl (which starts a watch app suspended) and brings it forward with `activate`.
  **Unverified**: the CI workflow's `watch` input and the agent using the watch from inside the sandbox (the socket is
  allowed and the daemon path works unsandboxed); the web UI has no watch option yet.

- **Daemon sockets, 2026-10-19**: the CI job's watch daemon failed with `OSError: AF_UNIX path too long`: macOS caps a
  socket path at 104 bytes and `runs/.tmp/daemon-watch.sock` under the runner's checkout came to 104 (the sim's, two
  shorter, fit). Sockets now live in `/private/tmp/th-<hash of the checkout>/<target>.sock` (`config.daemon_socket`,
  a directory of the user's own, mode 0700), and `ci/claude-sandbox.py` takes the paths from there; the agent sandbox
  may also read and write the temp folders (/tmp, $TMPDIR, $RUNNER_TEMP).

- **Loop with the watch, 2026-10-19**: LoopWorkspace `feat/tandem-closed-beta` cloned into `LoopWorkspace/` (771 MB with
  submodules) and built for the simulator in ~45 min on this Mac, first try. `trio-test-e2e --app loop --devices
  phone+watch` then ran clean on the 46 mm pair: Loop and its Watch app (`<Loop>.LoopWatch`, read from the build's
  embedded Watch app) installed, the Watch app before the phone app, Apple Health's sheet, CGM + Pump Simulator, the
  CGM's sine curve, and the phone's context on the watch (loop ring, glucose, eventual glucose). Fixed on the way, the
  Loop phone flows' first run on a Mac: Loop's HUD gives "Add CGM" / "Add Pump" as the value of "CGM Status" / "Pump
  Status", and refreshes it only with a reading, so on the simulator `offers` asks Loop's saved managers
  (`Documents/CGMManagerState.plist`, `PumpManagerState.plist`); iOS 26's Health sheet has its Allow at the bottom
  (`UIA.Health.Allow.Button`) and ignores most coordinate taps, so `Driver.tap_element` taps it as an element through
  WebDriverAgent. Watch side: after a Watch app is reinstalled, the watch's wcd filed Loop's WatchKit extension under
  its own id (`…LoopWatch.watchkitextension`) instead of Loop's, so Loop's context never reached it (the first install
  was right; Trio's single-target app was never affected); `WatchDriver.install` now restarts the watch's wcd.
  Loop Watch bolus end to end, twice: Bolus, the amount by crown (0.10 and 0.15 U), **Bolus**, then "Turn Digital
  Crown to bolus" confirmed by one full turn at one turn a second (`crown 1.0 --velocity 1`): the flow closed and Loop's
  log shows the Mock pump's bolus in progress at that amount and `enactBolus succeeded`. Smaller or slower turns move
  nothing visible there (Loop scales the confirmation by 4 at low sensitivity), which first read as a focus problem.
  Agent fixes found on the way: its heartbeat file was replaced by a remove and a move, so the harness could read the
  agent as stopped in between (now one rename(2)), and it was written only between commands, so a slow action looked
  like a dead agent (now its own thread). `home` through the agent verified here too (it was listed as verified on
  2026-10-18 without having run).

## Tandem pump on faketandem (`--pump faketandem`, 2026-10-25)
The app's TandemKit built on its virtual pump (`TANDEMKIT_VIRTUAL_PUMP`), against faketandem, a Tandem Mobi emulator
(README "Tandem pump on faketandem"; `harness/faketandem.py`). It has two setups: `preseed` starts the app paired,
and `walk` leaves the pump for the agent to add, following `ci/agent-faketandem-walk.md`. `--pump-scenario` gives the
pump hours of use first, through faketandem's scenarios (jwoglom/faketandem branch `claude/beautiful-dirac-aoq7i7`
until merged: pass `--faketandem-ref` until then).
- Verified on Linux, without a simulator: building faketandem from a checkout, starting it for either setup with a
  scenario, `th faketandem status|history|pairing|api|stop --save`, and the seeded `PumpManagerState.plist` (the key
  faketandem holds, its peripheral id) and basal profile.
- **Verified on the macOS workers, 2026-10-25** (Trio `feat/tandem-closed-beta` + jwoglom/TandemKit#609, scenario
  `recent-doses`, faketandem from source with Go via `actions/setup-go`): the virtual-pump build (the TSan-free scheme
  copy, `-virtualpump` DerivedData; build to launch in about 7 minutes). `walk` (run 40223372300): the agent added Tandem
  from Trio's pump picker, went through insulin type and sounds, saw "Found the Tandem Mobi 123" at the double-press
  step, ran `faketandem pairing PairStep2`, entered 123456 and paired. It then loaded the history on #609's Load Pump
  History screen. Trio's treatments list carried the pump's four boluses (the stopped one at 1.2 U delivered) and its
  two temp rates as reconciled basal, and Trio's IOB went from 0 to 2.88 U on its next determination (8.2 U of
  boluses). The pump showed 3.1 U under its own model. `preseed` (run 40223375850): the seeded driver authenticated
  with faketandem during the smoke stage, and Trio's pump screen showed the Tandem Mobi with faketandem's battery,
  cartridge and 0.85 U/hr. The agent reached faketandem through the sim daemon in both runs.
- A bolus from Trio in that run stopped at "Enter iPhone Passcode": with no enrolled face, Trio's
  `.deviceOwnerAuthentication` falls back to a passcode the simulator never takes. `trio-test-e2e` now enrolls a face
  at launch, and `th faceid match` answers the prompt. Verified in runs 40227024386 (`preseed`) and 40227028882
  (`walk`): Trio's 0.5 U bolus asked for Face ID, `faceid match` answered it, the home screen showed "Bolusing 0.2 of
  0.5 U", faketandem recorded the bolus delivered in full, and Trio finalized it at 0.5 U.
- What the `walk` run 40227028882 found in TandemKit#609, and what came of it:
  - The pump's 15-minute suspend in `recent-doses` did not reach Trio: the setup load reported boluses and basal
    corrections but none of the pump's suspends and resumes from before pairing. TandemKit's
    `DeliveryTransitionLedger` now reports them from the load's window on (`historySince`, on jwoglom/TandemKit
    `claude/beautiful-dirac-aoq7i7`). Verified in run 40232151326 (#609 with that commit cherry-picked through
    `submodule_cherry_pick`): Trio's treatments list PumpSuspend and PumpResume at the pump's times, with no basal
    inside the suspend, alongside the four boluses and the basal corrections.
  - Delete Pump, pair again and Load: Trio's own 0.5 U bolus came back from the pump's history under another
    identifier and was stored twice. Fixed on TandemKit#609 (see below).
  - A setup started from Settings ▸ Devices ends on the pump's settings rather than Setup Complete, whichever button
    the Load Pump History step was given: Trio's pump screen swaps to the settings as soon as the manager is handed
    over, which TandemKit does before Setup Complete. From the home screen's "Add pump", Setup Complete shows.
  - The Load step's progress screen was never on screen: the driver had read the whole log right after pairing, and a
    delay on `HistoryLogRequest` never fired. `ci/agent-faketandem.md` now says to delay `HistoryLogStatusRequest`;
    with 8 s on it, run 40232151326 screenshotted the progress screen at 99% before Setup Complete.
- In the first runs, `docs search` from the agent's sandbox failed with IncompleteRead on triodocs.org's search index
  (not faketandem's doing). `harness/triodocs.py` now asks for gzip, completes a cut-short body with Range requests
  (checked against a local server that cuts responses) and falls back to fetching through the harness daemon. Verified
  from the sandbox in run 40232151326: `docs search "pump settings"` found Settings ▸ Devices ▸ Insulin Pump.
- TandemKit#609's delete-and-re-pair fix. Run 40263901771 tested a first version at `047b40f`, which kept an archive of what
  the deleted manager reported; it was reverted for identity-keyed reporting (every dose under an id the pump assigns, so a
  second manager reports the same rows under the same ids). Run 40279063228 tested that at `14ba66c`: Trio set up the pump
  and loaded history, bolused 0.5 U, suspended and resumed, deleted the pump and set it up again with the load; Trio's 19
  stored pump events were unchanged although the second setup re-reported them all. It also found Trio's sheet left blank
  after the first Delete Pump (TandemKit `80c7bee` fixes it) and the deleted pump back after a relaunch (Trio keeps its saved
  pump manager; TandemKit's `TrioPumpDeletionTests` pins that). Run 40258291563 before them lost the simulator's display:
  its agent shut the simulator down to free a hung WebDriverAgent, and from the sandbox the display never came back.
  `ci/agent-prompt*.md` now forbid that.
- `report pr --body` and the e2e workflow's `comment_on_run`/`comment_body`: verified by run 40267059203, which posted a
  written comment on jwoglom/TandemKit#609 with 14 of run 40263901771's screenshots uploaded inline, and again by run
  40289321649 for run 40279063228.
- Not verified yet: `th faketandem seed` by hand; the web UI's pump fields; Loop with `--pump faketandem`.

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
  Harness git uses SSH by default; when no key is loaded in ssh-agent (seen 2026-10-06 in Claude Code shells), use
  `TH_GIT_PROTOCOL=https` / `th --git-protocol https`, which authenticates through that gh login.

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
