# TandemKit stress lane: Trio on the simulator against faketandem

`th stress run` looks for crashes, data races, hangs and failures to recover in TandemKit while it runs inside
Trio, by putting the driver under load and breaking its link on purpose. It builds Trio with its TandemKit
submodule moved to TandemKit `dev` (or any ref or PR), compiled with `TANDEMKIT_VIRTUAL_PUMP` and ThreadSanitizer.
The app runs on the iOS Simulator against [faketandem](https://github.com/jwoglom/faketandem), the Go Tandem pump
emulator. Seeded chaos then runs for the rest of the time budget: pump-side events, link faults, app kills and UI
noise, each round ending in a check that the driver came back. Only simulated devices are involved.

```
th stress run                                    # nightscout/Trio feat/tandem-closed-beta + TandemKit dev, 50 min budget
th stress run --minutes 25 --seed 42 --intensity high
th stress run --tandemkit jwoglom/TandemKit#470  # a TandemKit PR
th stress run --app <Trio.app> --minutes 15      # reuse a virtual-pump build, skip checkout + build
th stress file-issues runs/<run>                 # one GitHub issue per new finding (CI does this)
th stress scan app/launch-03.stderr crashes/*.ips  # triage: the findings in sanitizer output / crash reports
th stress faketandem --paired                    # the emulator alone, for poking at it by hand
```

## How a run goes

| Stage | What happens | Code |
|---|---|---|
| checkout + build | `prepare_source(branch=--trio, submodules=[TandemKit@--tandemkit])`, then `xcodebuild` with `TANDEMKIT_VIRTUAL_PUMP` and `ENABLE_THREAD_SANITIZER[sdk=iphonesimulator*] = YES` in the harness xcconfig. The sanitizer is set per SDK because Trio's scheme embeds a watchOS app, which the sanitizers do not support. A stress build never touches `build/last-build.json`. Its own xcconfig is `build/override-sim-tandemkit_virtual_pump-sanitized.xcconfig`. The build is stopped at the deadline, so a CI job's limit never kills it without a report. | `harness/build.py` |
| faketandem | Built from `TH_FAKETANDEM_REPO`@`--faketandem-ref` (or `TH_FAKETANDEM_BIN`) and started on two free loopback ports. Set to `PairStep2` with a random 32-byte long-term key over its websocket. | `harness/faketandem.py` |
| simulator | Fresh install, onboarding pre-seeded, and three files written into the container. `Documents/PumpManagerState.plist` holds a TandemPumpManager already paired with that key. `freeaps/freeaps_settings.json` sets closed loop and the Glucose Simulator. `preferences.json` and `settings/` set max IOB 3, SMB always, and a basal of 0.85 U/hr to match faketandem. Settings overlay the defaults bundled in the built app. | `harness/stress/seed.py` |
| launch | `simctl launch --stdout/--stderr=<run>/app/launch-NN.*` with `SIMCTL_CHILD_TANDEMKIT_VIRTUAL_PUMP=<faketandem>` and `TSAN_OPTIONS`, plus the toolchain's clang runtime directory as `DYLD_FALLBACK_LIBRARY_PATH` in case the sanitizer runtime was not embedded. TandemKit's OSLog is streamed to `tandemkit-oslog.txt`. WebDriverAgent comes up if it can, and the Glucose Simulator is added through the UI if the seeded setting did not take. | `harness/stress/app.py`, `lane.py` |
| rounds | `--round-minutes` of chaos, then every fault undone, then the recovery check. A dead app is a finding and is relaunched. A slow accessibility query from a live app is sampled as a possible hang. | `chaos.py`, `recovery.py` |
| collection | Sanitizer and Main Thread Checker reports from every launch's stderr. `Trio*.ips` from `~/Library/Logs/DiagnosticReports`. TandemKit's and Trio's log files. faketandem's request log, appended per round to `faketandem-requests.jsonl`. | `detect.py`, `lane.py` |

How the pump gets paired without UI:
- A restored TandemKit with `derivedSecret` + `pincode` authenticates through JPAKE rounds 3/4 only, which is what faketandem accepts for a key given over `setLongTermKey`. It learns `serverNonce` on that first connect.
- It finds the pump by scanning for `peripheralUuid`, compared as a string with `CBPeripheral.identifier.uuidString`. So the seed uses the `peripheral_id` from faketandem's `hello` frame, in upper case.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Clean. Also used for `built-only` / `build-incomplete`: the build used the budget, and the next run starts warm. |
| 1 | Findings. |
| 2 | Harness or setup failure. |
| 3 | Trio does not build against that TandemKit. |

Each run leaves `summary.md`, `result.json` and `findings.json` in `runs/<ts>-stress-<ref>/`.

## What counts as a finding

| kind | detected by | fingerprint |
|---|---|---|
| `tsan` / `asan` | sanitizer reports on the app's stderr | report kind + the top 3 non-system frames of the access and the conflicting access (allocation, thread creation and mutex stacks are left out) |
| `main-thread` | Main Thread Checker on stderr (`--main-thread-checker`) | API + top 3 app frames |
| `crash` | `Trio*.ips` written during the run (skipping SIGKILL/SIGTERM of a pid the harness ended) | exception + top 4 non-system frames of the crashed thread |
| `app-died` | the app's pid gone without the harness ending it | (one per run; the crash report carries the detail) |
| `no-recovery` | after a round's faults are undone, either no authenticated session within `--session-timeout`, or a `remainingInsulin` qualifying event (whose handler only reads `InsulinStatusRequest`) not answered within `--probe-timeout` | the stage that failed |
| `hang` | a WebDriverAgent query to the live app slower than `--hang-seconds`; `sample` of the process | deepest 3 app frames of the main thread |
| `build` | xcodebuild fails | first compiler error, paths and line numbers removed |

Fingerprints use function names only, so the same race on a later commit, or with the code moved a few lines, maps
to the same issue. Frames are classed as TandemKit / Trio / LoopKit / other from module and path, and the class is
the finding's `component`.

## Chaos

A seeded `random.Random` picks every action and its parameters (`--seed`, recorded in `run.json` and every
issue). The seed reproduces the plan, not the interleaving.

| category | actions |
|---|---|
| pump | pump-UI or "remote" bolus (0.05–2 U), bolus abort, suspend (user/occlusion/alarm; resumed 20–150 s later), pump-side temp basal, bursts of read-only qualifying events, battery/reservoir changes, pump clock skew (±15 min), Control-IQ on/off |
| link | `drop_response`, `delay_response` (to 8 s), `error_response` (not on InitiateBolus), disconnect after a request or mid-response, `drop_fragment`, radio off for 2–90 s, pump-side link drop, ATT MTU 23/185/247 |
| app | SIGKILL then relaunch, `simctl terminate` then relaunch, background for 3–45 s |
| ui | Settings ▸ Devices ▸ Insulin Pump (TandemKit's settings screen), tab hops |

`--intensity` sets the gap between actions: low is 10–40 s, medium 3–15 s, high 0.5–5 s. `--categories` limits
the families. App actions need the simulator, and UI actions need WebDriverAgent. Without WDA (`--no-ui`, or it
did not come up), the run continues with pump, link and app chaos.

## CI

`.github/workflows/stress-sim.yml` runs nightly (06:23 UTC) and on demand, on the self-hosted macOS workers.

- **Time budget.** The job has a 60-minute limit, and the run gets a deadline 52 minutes after the job starts. The
  build is stopped at whatever leaves time for setup and one round. The rest goes to rounds, then issues and
  artifacts.
- **Persistent cache.** `TRIO_REPO`, `TH_DERIVED_DATA` and `TH_BUILD_DIR` live in a cache that survives between
  jobs: `/Volumes/work/cache/trio-stress` if writable, else `~/Library/Caches/trio-test-harness-stress`. A cold Trio
  build with TSan does not fit in the hour. An incremental one, where usually only TandemKit changed, does. The
  first run on a runner may only build; that is reported as `build-incomplete` or `built-only`, not as a failure.
- **Issues.** Findings (exit 1) and build breaks (exit 3) go to this repository through `th stress file-issues`
  with the workflow's `GITHUB_TOKEN` (`issues: write`):
  - one issue per fingerprint, marked `<!-- stress-fingerprint: … -->`, labelled `stress` and `stress:<kind>`
  - a repeat comments on the open issue
  - a fingerprint whose issue was closed gets a new issue that links the old one
  - bodies are scrubbed of long hex/base64 strings and home paths
  - nothing is filed in TandemKit; a confirmed driver bug is promoted there by hand
- **Artifacts.** The whole run directory, kept for 14 days.

## Emulator gaps that shape the chaos

- faketandem accepts `InitiateBolusRequest` while a bolus runs or delivery is suspended. A real pump refuses. The
  lane has no dose oracle yet, so this skews nothing it checks today.
- An armed `error_response` is applied after the handler ran (TandemKit `docs/integration-harness.md`, D3), so
  the chaos never arms it on InitiateBolus.
- `POST /api/state/tempbasal/start` drops `percent: 0` (`omitempty`), so a pump-side 0% temp basal cannot be
  staged.
- Messages faketandem sends natively (history log stream, alarm status, qualifying events) bypass message-level
  faults and the request log; only `drop_fragment` reaches them.
- `delay_response` sleeps on the connection's serial worker and delays everything queued behind it.
- One attached central per emulator process: a second app instance would be refused as `busy`.
- No Bluetooth power cycling on the phone side: TandemKit's virtual `CBCentralManager` is always `.poweredOn`.
  Radio faults are the pump's radio.

## Not verified on a Mac yet

The faketandem side, the chaos actions, the recovery check, the detectors and the issue bodies were run on Linux:
- against a real faketandem
- against a real TandemKit driver, the Linux TSan build of TandemKit's integration target, running a host loop
  while the chaos ran

Everything that needs Xcode was written from documentation and code reading. The first run on a worker should
confirm:
- the SDK-conditional `ENABLE_THREAD_SANITIZER` setting producing a TSan-instrumented Trio (and leaving the watch app alone)
- `simctl launch --stdout/--stderr` capturing the app's TSan output
- `SIMCTL_CHILD_*` reaching the app
- Trio restoring the seeded pump and honouring the seeded settings (`summary.md` and `run.json`
  `trio_settings_after` show what Trio kept)
- the bundled defaults living at `Trio.app/json/defaults/`
- simulator crash reports landing in `~/Library/Logs/DiagnosticReports`
- the Main Thread Checker dylib path (hence opt-in)
