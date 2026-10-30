# trio-test-harness

Tooling + Claude Code skills to **build, install, drive and screenshot the Trio iOS app** (and **Loop**, with
`--app loop`: see [Loop](#loop---app-loop)) on either the iOS Simulator or a USB-attached iPhone, from any branch / PR /
submodule branch, and to turn the screenshots into an HTML report (claude.ai Artifact) or a GitHub PR comment.

```
./trio-test-e2e --mode simulator --pr https://github.com/nightscout/Trio/pull/1234     # Trio PR
./trio-test-e2e --mode rooted-vnc --pr https://github.com/jwoglom/TandemKit/pull/395          # submodule PR (Trio @ feat/tandem)
./trio-test-e2e --app loop --pr https://github.com/jwoglom/TandemKit/pull/395     # the same PR in Loop (LoopWorkspace)
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

## Loop (`--app loop`)

Every stage also runs Loop, built from LoopKit/LoopWorkspace: `--app loop` on `trio-test-e2e`, `th` and `trio-test-ci`,
`app=loop` in the CI workflow, "Loop" in the web UI form, or `TH_APP=loop` in the environment or `harness.env` for a
machine that tests Loop by default. What differs between the apps is in `harness/apps.py` and `harness/flows/loop.py`:

| | Trio | Loop |
|---|---|---|
| Checkout | `TRIO_REPO`: nightscout/Trio, base `dev` | `LOOP_REPO`: LoopKit/LoopWorkspace, base `feat/tandem-closed-beta` (the branch with the TandemKit submodule) |
| Build | `Trio` scheme, `TH_DERIVED_DATA` | `LoopWorkspace` scheme, `TH_LOOP_DERIVED_DATA` (the workspaces build different forks of the same frameworks) |
| Bundle id, app group | `org.nightscout.<team>.trio`, `group.<bundle>.trio-app-group` | `com.<team>.loopkit.Loop`, `group.<bundle>Group`; `MAIN_APP_BUNDLE_IDENTIFIER` is written out, since an ad hoc build clears the `DEVELOPMENT_TEAM` Loop derives it from |
| Skip onboarding, simulator | `Documents/onboardingCompleted.plist` | the app group's preferences: `OnboardingManager.IsComplete` and LoopOnboarding's mock therapy settings, as the legacy keys Loop migrates at launch; then the simulator's `cfprefsd` is restarted |
| Skip onboarding, phone | flags over SSH | holding the welcome screen's logo for 2 s (LoopOnboarding's own skip, with the same mock settings) |
| After onboarding | the home screen | prompts for notifications, Apple Health ("Turn On All", "Allow") and Bluetooth, which `walk_onboarding` answers |
| Fake devices | Glucose Simulator + Pump Simulator, from the home header | CGM Simulator + Pump Simulator, from the HUD's "Add CGM" / "Add Pump" (else Settings); a new CGM Simulator has no glucose model, so on the simulator the smoke stage gives it a sine curve |
| `th logs` | Trio, TandemKit, watch | TandemKit only: Loop's own device log is an SQLite store |
| `th docs` | https://triodocs.org | LoopDocs, https://loopkit.github.io/loopdocs/ |

A PR on a LoopWorkspace submodule (TandemKit, or Loop itself, LoopKit/Loop) is found through `.gitmodules` of
`--trio-branch`, the base branch, then `feat/tandem-closed-beta`, `dev` and `main`, as for Trio:

```
./trio-test-e2e --app loop --pr https://github.com/jwoglom/TandemKit/pull/395
./trio-test-e2e --app loop --pr LoopKit/Loop#2500 --trio-branch dev
./trio-test-ci --app loop -s jwoglom/TandemKit#395
```

On the simulator a new install also empties Loop's app group container, which the simulator keeps across reinstalls
and where Loop keeps its store and settings. The Loop flows are written from Loop's source and have not yet run against a
real Loop on a Mac: `docs/HANDOFF.md` lists what to check.

## Apple Watch (`--target watch`)

The app's Watch app (Trio's, or Loop's with `--app loop`) runs on a watchOS simulator paired with the harness's iPhone
simulator, so a Watch change can be tested the way a phone change is. `trio-test-e2e --devices phone|phone+watch|auto` (`--watch` = phone+watch; CI's `devices`
and `watch_model` dropdowns, `trio-test-ci --devices … --watch-model 40mm`, the web UI's Devices field) says whether a run includes it. **phone** is the default. **auto** asks a small model
(Haiku through `claude -p`, `TH_SCOPE_MODEL`) whether the change can alter anything the watch shows or does, from every PR
of the run: the app's (Trio or LoopWorkspace), the submodule PR, other submodule PRs and cherry-picked PRs, with their
titles, descriptions and changed files (`harness/watchscope.py`, ~10 s); without Claude the changed paths decide. The
answer and its reason go into `pr.md` and `run.json`. In CI, auto needs `CLAUDE_CODE_OAUTH_TOKEN` in the build step too.
With the watch, `trio-test-e2e` does the setup on the watch model's own **phone + watch
pair**: `TrioHarness (<model>)` and `TrioHarnessWatch (<model>)` (`TH_WATCH_SIM_DEVICE`, default Apple Watch Series 11
(46mm); `TH_WATCH_SIM_RUNTIME`), created on first use and paired once, while both are shut down (`simctl pair`: Xcode ships
no pre-paired simulators). From then on that phone is the simulator every command drives. A phone simulator that is paired
again or erased stops linking Watch apps to iPhone apps, so a pair is never re-paired, and `th watch pair --recreate`
makes a broken one anew (`docs/HANDOFF.md`). It installs the build on the phone and then its embedded Watch app on the
watch, launches the Watch app **before** the phone app (Trio's phone session looks for the watch when it starts) and
screenshots it at home and once the phone's glucose reaches it. Then every primitive takes `--target watch`, plus `th --target watch crown <turns>` for the Digital
Crown. By hand: `th watch pair`, `th --target watch install`, `th watch agent start`, `th --target watch launch`,
`th launch`.

| | |
|---|---|
| Screenshots | `simctl io screenshot` of the watch (416×496 px on the 46 mm watch) |
| Taps, tree, crown, Home | the watch agent, `harness/watchagent`: a watchOS XCUITest runner (`XCUIDevice.rotateDigitalCrown`, `XCUIElement.snapshot`) run with `xcodebuild test-without-building` beside WebDriverAgent, which has no watchOS runner. Built on first use into `build/watchagent-dd`; `th watch agent build|start|stop|status` |
| Harness ↔ agent | JSON files in `runs/.tmp/watch-agent` (`cmd-<id>.json` in, `res-<id>.json` out, `alive.json` every second): a simulator process always reaches the Mac's file system, while sockets on watchOS are restricted |
| Phone ↔ watch | real WatchConnectivity between the two simulators: messages, application context and user info all arrive |
| Lock | the watch shares the simulator's device lock (`th busy`) |

Watch model and settings: the model is chosen when the watch starts, `--watch-model 40mm` or `th watch pair --model 40mm`: a size (40mm, 41mm, 42mm,
44mm, 45mm, 46mm, 49mm or ultra) picks one model per screen size Trio lays out for, or give a full model name
(`th watch model` lists both). Another size is another pair, so another phone simulator
whose Trio keeps its own data (a new one needs its fake CGM and pump); starting it shuts the pair in use down, and the
harness never boots more than one iPhone and one watch simulator of its own. The settings change on the running watch:
`--watch-wrist`, `--watch-crown`, `--watch-text-size` (or `th watch settings`). Wrist and crown
are the watch's `com.apple.nano` `wornOnRightArm` / `invertUI`, text size UIKit's preferred content size category
(`simctl ui content_size` refuses watchOS); apps read them at launch, so changing one relaunches the Watch app, never the
simulator.

What the phone did with a Watch request is in `th logs trio --grep WatchManager`; the Watch app's own log reaches
`th logs watch` only when the watch flushes it to the phone (every 3 minutes or 100 entries). The Trio Watch facts the
agent relies on are in the `trio-ui-drive` skill.

## Tandem pump on faketandem (`--pump faketandem`)

To test TandemKit without a pump, the app's pump can be a Tandem Mobi emulated by
[faketandem](https://github.com/jwoglom/faketandem). This is the setup TandemKit's own `TrioIntegrationTests` use. The
app is built with `TANDEMKIT_VIRTUAL_PUMP`, so TandemKit's stand-in for CoreBluetooth reaches the pump over a local
socket instead of the radio. Pairing, TandemKit's setup and settings screens, and everything the driver exchanges with
the pump then run for real, against the emulator.

```
./trio-test-e2e --mode simulator --branch feat/tandem-closed-beta --pump faketandem                      # pre-paired
./trio-test-e2e --mode simulator --pr https://github.com/jwoglom/TandemKit/pull/609 --trio-branch feat/tandem-closed-beta \
    --pump faketandem --pump-setup walk --pump-scenario recent-doses     # the agent walks the setup; 6 h of pump history
./trio-test-ci feat/tandem-closed-beta -s jwoglom/TandemKit#609 --pump faketandem --pump-setup walk --pump-scenario recent-doses
```

| | |
|---|---|
| Build | `SWIFT_ACTIVE_COMPILATION_CONDITIONS += TANDEMKIT_VIRTUAL_PUMP` (Debug only), in its own DerivedData (`<TH_DERIVED_DATA>-virtualpump`), so switching does not rebuild the plain build's targets. Trio is built from a copy of its scheme without ThreadSanitizer, which Trio's Run action turns on: a race report can freeze the app (TandemKit `scripts/trio-test.sh` does the same) |
| faketandem | built with Go from `TH_FAKETANDEM_REPO` (jwoglom/faketandem) at `--faketandem-ref` / `TH_FAKETANDEM_REF` (main), or `TH_FAKETANDEM_DIR` / `TH_FAKETANDEM_BIN`; without Go, the `latest` release binary. One runs per checkout in the background (`runs/.tmp/faketandem.json`), on free loopback ports, and outlives `trio-test-e2e` so the agent can use it |
| Launch | every simulator launch (`th launch`, the daemon's, `db exec`'s relaunch) passes `TANDEMKIT_VIRTUAL_PUMP=<faketandem>` (`SIMCTL_CHILD_`). An app started any other way, such as from its icon after it was stopped, has no address and reports Bluetooth as unsupported |
| `--pump-setup preseed` (default) | the app starts with the pump paired. faketandem holds a random long-term key, and the app's `Documents/PumpManagerState.plist` holds a `TandemPumpManager` after a pairing with it (the key, the peripheral id from faketandem's `hello`, the 0.85 U/hr schedule). The smoke stage waits up to 2 minutes for the driver to authenticate. As in TandemKit's `TrioClosedLoopTests` |
| `--pump-setup walk` | the pump is left to be added in the app (Trio: Add pump ▸ Tandem). faketandem waits at the double-tap step (`PairStep1`). `th faketandem pairing PairStep2` makes it show its code, `123456`. The CI agent is told to walk the setup (`ci/agent-faketandem-walk.md`) |
| `--pump-scenario` | faketandem's `-scenario`: hours of pump use replayed before the app connects, giving history, IOB, a last bolus and perhaps a running temp basal, with a BasalDelivery record every 5 minutes at the rate in force. A preset (below; a dropdown in the web UI and the CI workflow) or a JSON file |
| App settings | the app's basal schedule is seeded at faketandem's 0.85 U/hr profile, so neither TandemKit's basal check nor its history reconciliation finds a difference the harness made |

The presets (`SCENARIOS` in `harness/faketandem.py`; each is a file in faketandem's `pkg/harness/scenarios`, whose
README describes it in full). All but `pump-profile-differs` run on the app's 0.85 U/hr profile. IOB is the pump's
own figure with Control-IQ off.

| Preset | What the pump did before the app connects |
|---|---|
| `recent-doses` | 6 h: boluses from the pump and from an app (the last stopped at 1.2 of 2 U), a 0% temp, a suspend and resume, a 150% temp still running; about 3.2 U IOB |
| `scheduled-basal` | 6 h of the profile's basal alone; no bolus, no IOB |
| `pump-boluses` | 6 h: four boluses from the pump's screen only, the last 15 minutes ago; about 4.9 U IOB, none from an app |
| `app-doses` | 6 h driven by another app over Bluetooth: app boluses and app temps (with their `SetTempRateResponse` records), one temp stopped early, a 50% temp still running; about 1.5 U IOB |
| `suspended-now` | 6 h: two pump boluses, then suspended from the pump's screen 45 minutes ago and still suspended; about 2.0 U IOB |
| `stopped-boluses` | 6 h: boluses stopped part-way, the last stopped before it delivered anything (`LastBolusStatus` reads 0 U); about 2.7 U IOB |
| `control-iq` | 6 h with Control-IQ on and still on: its rate moving between 0 and 1.4 U/hr, two automatic boluses and one from the pump; TandemKit's setup turns Control-IQ off, so use it with `--pump-setup walk` |
| `pump-profile-differs` | 6 h on a six-segment pump profile (0.6–1.1 U/hr) that is not the app's: TandemKit writes the app's schedule over it, and a setup history load finds basal the app did not count |
| `day-of-use` | 24 h of boluses, temps and a suspend: four times the 6 h a setup history load reads |

The presets past `scheduled-basal` need a faketandem that has them (`--faketandem-ref` until they are on its `main`).

`th faketandem start|stop|status|seed|pairing|history|scenario|api` controls the emulator and reads what the pump
holds (`th faketandem --help`). The CI agent reaches it through the sim daemon, because the agent's sandbox has no
network. `stop --save` writes the pump's state, history log, request log and log into the run's `faketandem/`, and CI
and the web UI do that at the end of a job. In CI: `pump`, `pump_setup`, `pump_scenario` and `faketandem_ref`, with
Go set up by `actions/setup-go`. In the web UI: "Pump (simulator)". Only emulated values are used. faketandem's
serial number, key and history are made up.

## How it works

Three connection modes (`--mode` / `--target`):

| Piece | `simulator` | `rooted-vnc` (jailbroken iPhone) | `iphone-usb` (any iPhone, no root) |
|---|---|---|---|
| Build | `xcodebuild`, ad-hoc signing, same flags as Trio CI | unsigned build fake-signed with `codesign -s -` → `Trio.ipa` | automatic signing (`TH_DEVELOPER_TEAM`, Apple ID in Xcode) = `th build --xcode` |
| Install / launch | `simctl install/launch` | `pymobiledevice3 apps install` via **AppSync Unified**; `uiopen` over SSH | `xcrun devicectl` install (`th install --xcode`); launch via Spotlight (⌘3) in the mirror |
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

Git transport: every GitHub URL (Trio, forks, private submodules such as TandemKit) is rewritten to one protocol,
`TH_GIT_PROTOCOL=ssh` (default; needs your key in ssh-agent) or `https` (authenticates through the `gh` login). Override per
command with `th --git-protocol https …` or `trio-test-e2e --git-protocol https …`; `th doctor` reports whether it works.

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
  or TrollStore (iOS ≤ 17 only) via `--trollstore`.
- Details: `docs/phone-setup.md`.

Any iPhone, no jailbreak (**Xcode install**): `th build --xcode` signs with your team (the checkout's `ConfigOverride.xcconfig`,
else `TH_DEVELOPER_TEAM`; Apple ID in Xcode), then `th install --xcode [--device <udid|name>]` installs it with
`xcrun devicectl`, as Xcode's Run does. Default device: `TH_XCODE_DEVICE`, else the only paired iPhone on USB. Needs Developer
Mode, and the phone unlocked while installing. Install only — driving the phone needs `rooted-vnc` or `iphone-usb`.

`iphone-usb` (macOS iPhone Mirroring, built on [phone-harness](https://github.com/ShawnPana/phone-harness), vendored under `vendor/`):
- Mac and iPhone on the **same Apple ID**, Bluetooth + Wi-Fi on, iPhone Mirroring paired once (open the app, confirm on the phone), phone **locked** while mirroring.
- Terminal granted Accessibility + Screen Recording. `th device mirror-status` shows the window; `th --target iphone-usb shot` captures it.
- Installing needs a properly signed build (`TH_DEVELOPER_TEAM`) and Developer Mode on the phone; onboarding is walked (no root to pre-seed).

## CLI overview (`./trio-test-harness --help`; `th` = short alias in `bin/`)
`doctor` · `build` · `install` · `launch` · `sim {list,runtimes,install-runtime,boot,shutdown,erase,container,defaults,openurl}` ·
`device {list,vnc-check,serve-ipa,wake,ssh,diag,vnc-restart,kill,apps}` · `faketandem {start,stop,seed,status,pairing,history,scenario,api}` · `faceid {enroll,match,nomatch}` · `daemon {start,stop,status,restart}` · `wda {build,start,stop,status}` · `watch {pair,status,agent}` · `shot` · `tap` · `swipe` · `drag` · `scroll` · `type` · `key` · `home` · `crown` · `tap-text` · `find` ·
`tree` · `wait-text` · `ocr` · `logs [trio|tandem|watch|all]` · `db {stores,tables,schema,query,exec}` · `oslog` · `crashes` · `files {ls,cat}` · `perf` · `run {new,current,use,note,finish,list,steps}` · `report {html,pr,markdown}` ·
`flow {onboarding-walk,add-cgm-sim,add-pump-sim,back-home,dismiss,at-home}` · `test smoke`

Runs live in `runs/<timestamp>-<name>/` (`screenshots/`, `steps.jsonl`, `run.json`, `report.html`).

`th docs search "<words>" | toc | page <path or URL>[#section]` reads https://triodocs.org (Trio's MkDocs documentation) as
text: search runs over the site's own search index (every section's title and text) and prints links and snippets, `toc`
lists all pages as a tree (from the sitemap), `page` prints a page's article (headings, lists, tables, admonitions,
links) or one section. Pages are cached for a day in `runs/.tmp/triodocs/`. The web UI/CI agent's sandbox allows
`triodocs.org` (and nothing else) for it, and the agents are told to look settings up there before navigating.
Through the sandbox's network proxy the 640 KB search index arrived cut short (IncompleteRead), so pages are fetched
gzipped, a plain body cut short is completed with Range requests, and failing that a running harness daemon, outside
the sandbox, fetches it.

`th logs` reads the installed app's log files: Trio's `Documents/logs/log.txt`, TandemKit's
`Documents/tandem/tandem_log.txt` (info and above; debug goes to OSLog only) and the watch log, plus each one's `*_prev`
file for the previous day. It filters with `--grep REGEX [-i]`, `--since 10m|2h|<ISO time>` and `--level warning`
(TandemKit), prints the last `-n` entries (default 200; multi-line entries stay whole, `all` merges by time), lists sizes
with `--files`, and copies the files into the run's `logs/` with `--save`. It reads from the simctl container or over SSH,
or through the target's daemon when one runs, so the sandboxed web UI/CI agent can use it (restart a daemon started
before this command existed). Logs from the phone may hold real device data: keep them out of reports and PRs.

`th db` reads and changes the app's local database, its Core Data SQLite store on the simulator (Trio's
`Library/Application Support/TrioCoreDataPersistentContainer.sqlite`, Loop's `Model.sqlite` in its app group; `--store`
picks another, e.g. Loop's `DeviceLog/Storage.sqlite`): `tables`, `schema ZTABLE`, `query "SELECT …"` (read-only) and
`exec "UPDATE …"`, which terminates the app, copies the store into the run's `db/` before the run's first change, runs the
SQL in one transaction, records a run step and relaunches the app. For states the simulators cannot produce (e.g. a bolus
programmed on the pump). The web UI/CI agent's sandbox may read and write only that app's simulator containers
(`ci/claude-sandbox.py` looks them up when it writes the settings, after the install).

More debugging (`harness/appdebug.py`; simulator and rooted-vnc, through the daemon so the sandboxed agent can use them):
- `th oslog [--since 10m] [--grep RE -i] [--level debug] [--subsystem S] [--all] [--save]`: the system log of the app's
  process, its frameworks included (TandemKit, LoopKit); Apple's own subsystems only with `--all`. Simulator: `log show`
  over its store (minutes on a small Mac); `--stream 30` records 30 s live with debug messages, and `--enable-debug`
  makes the store keep the app's and TandemKit's debug messages. Phone: a `.logarchive` collected over USB.
- `th crashes [--since 1d] [--save]`: the app's crash reports since the run started, summarised (exception, termination
  reason, crashed thread). `--save` copies them into the run's `crashes/` with Mac/phone identifiers removed, one step each;
  `trio-test-e2e`, the CI job and the web UI do that after their app sessions. Simulator crashes come from this Mac's
  `~/Library/Logs/DiagnosticReports`, the phone's from `pymobiledevice3 crash pull`.
- `th files ls [dir]` / `th files cat <path> [--save]`: the app's `Documents/` (Trio's OpenAPS JSON state such as
  `monitor/iob.json`, `settings/profile.json`; property lists print as JSON).
- `th perf [--watch 120 --interval 5] [--save]`: pid, CPU %, resident and physical-footprint memory, threads; `--save`
  appends to the run's `perf.csv`. `--leaks` (simulator) runs `leaks`, slow, and on builds without get-task-allow it sees
  only read-only memory, so its count is a lower bound.

## PR reporting
`./trio-test-harness report pr <PR> [--artifact-url <link>] [--max-shots N]` posts a comment whose screenshots are uploaded
*into* the comment with `gh pr comment --attach` (gh ≥ 2.99; up to 50 files, evenly thinned when a run has more). No extra
repo or hosting. `--mode description` maintains a `<!-- trio-test-harness -->` section in the PR body instead.
`--body FILE` posts a comment written for the run in place of the generated one; each screenshot it shows as
`![caption](./screenshots/NNN.png)` is attached the same way. In CI, the e2e workflow's `comment_on_run` (a run id) and
`comment_body` do this for an earlier run on the PR the run's inputs name, and test nothing:
`gh workflow run e2e.yml -f submodules=jwoglom/TandemKit#609 -f comment_on_run=<run id> -f comment_body="$(cat comment.md)"`.

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
  agent focus, agent time limit, post PR comment, simulator device/runtime, Xcode, and the pump: `pump=faketandem`
  with `pump_setup`, `pump_scenario` and `faketandem_ref` (see "Tandem pump on faketandem").
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
HTTPS instead of SSH (a token overrides `TH_GIT_PROTOCOL`), and uses the workers' git mirrors (`TH_GIT_MIRRORS`) and prebuilt WebDriverAgent
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
