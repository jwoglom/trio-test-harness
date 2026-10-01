---
name: trio-stress
description: Run, read and triage the TandemKit stress lane (`th stress`) — Trio on the iOS Simulator with TandemKit built against faketandem (the Tandem pump emulator) under ThreadSanitizer, with seeded chaos (pump-side events, link faults, app kills) and recovery checks. Use when asked to stress test / fuzz TandemKit, look for races or crashes in the Tandem driver, run or re-run the nightly stress job, reproduce a stress finding from its seed, or triage a `[stress]` issue.
---

# trio-stress

`cd ~/repos/trio-test-harness`; `th` = `./trio-test-harness`. Full description: `docs/stress.md`.

## Run
| Want | Command |
|---|---|
| The nightly configuration locally (closed-beta Trio + TandemKit dev, TSan) | `th stress run` (default budget 50 min, build included) |
| Shorter / reproducible / harsher | `th stress run --minutes 25 --seed 42 --intensity high` |
| A TandemKit branch or PR | `th stress run --tandemkit my-branch` · `--tandemkit jwoglom/TandemKit#470` |
| Skip checkout + build (an app already built with the virtual pump) | `th stress run --app <path to Trio.app>` |
| Only some chaos | `--categories pump,link` (also `app`, `ui`); `--no-ui` skips WebDriverAgent |
| In CI | `gh workflow run stress-sim.yml -R jwoglom/trio-test-harness -f tandemkit=… -f seed=…` |

The run holds the simulator's device lock (`th busy`); `--wait` queues behind another holder. Needs Go and Java 17
(faketandem + pumpX2's cliparser) or `TH_FAKETANDEM_BIN`/`TH_FAKETANDEM_JAR`; `th doctor` checks.

## Read a run (`runs/<ts>-stress-<ref>/`)
- `summary.md`: verdict, commits, seed, findings table, per-round recovery.
- `findings.json`: kind, component, title, fingerprint, signature, excerpt, evidence paths.
- `actions.jsonl`: every chaos action with round, parameters and result (`ok` / `skipped` / `error`).
- `app/launch-NN.stderr`: the app's stderr per launch (TSan reports land here); `crashes/*.ips`; `hangs/*.txt`.
- `tandemkit-oslog.txt` (TandemKit debug log), `logs/tandem_log.txt`, `faketandem.log`,
  `faketandem-requests.jsonl` (every message the pump saw, with seq numbers).
- Exit / `result.json` status: 0 clean or built-only, 1 findings, 2 harness failure, 3 Trio does not build.

## Triage a finding
1. `th stress scan <stderr|.ips|sample file> -v` prints the finding(s) and the frames the fingerprint uses.
2. For `no-recovery`, read the actions just before the failed round in `actions.jsonl`, then the request log
   around it: did the driver reconnect (`kind: connection`), authenticate (`Jpake3/4`), answer the probe
   (`InsulinStatusRequest`)? `tandemkit-oslog.txt` has the driver's side.
3. Reproduce with the issue's command (same TandemKit sha and seed). The seed fixes the plan, not the timing:
   run it a few times before calling it fixed or flaky.
4. A confirmed driver bug gets a TandemKit issue or fix PR that links the `[stress]` issue; a deterministic
   regression test belongs in TandemKit's unit or integration suites.

Only simulated devices are involved (faketandem, Glucose Simulator); still keep generated keys out of anything
posted — `th stress file-issues` scrubs them.
