## The pump: a Tandem Mobi emulated by faketandem

The pump in this run is not the Pump Simulator. It is a Tandem Mobi emulated by faketandem
(https://github.com/jwoglom/faketandem), and the app's TandemKit driver talks to it over a local socket instead of
Bluetooth (a "virtual pump" build of the same driver). So, for this pump, pairing, TandemKit's setup and settings
screens, and everything the driver reads from or sends to the pump can be tested here. What the rules above say about
the Pump Simulator and Bluetooth does not apply to it. Every value is made up: the pump's serial number, pairing code
and history are the emulator's.
`pr.md` says how the pump was set up for this run ("Pump:" line). These commands run inside your sandbox, through the
harness daemon:
- `./trio-test-harness faketandem status`: what the pump holds now. That covers the pairing state and code, the basal
  rate (profile, temp or suspended), the IOB the pump shows, its last bolus, the reservoir, and whether the app is
  connected. `--json` gives everything.
- `./trio-test-harness faketandem history`: the pump's history log of boluses, temp rates, suspends and resumes,
  oldest first. `--all` gives every record as JSON, the BasalDelivery records written every 5 minutes included.
- `./trio-test-harness faketandem pairing PairStep2`: the pump now shows its pairing code. This is the step after the
  user double-taps the pump's button.
- `./trio-test-harness faketandem cgm`: the pump's CGM sensor session (G6 or G7), which TandemKit's CGM Settings
  screen (the pump's settings ▸ CGM Settings) pairs, starts and stops. The pump starts with a G6 session a day old.
  `cgm reset g7` leaves it with no session; the app's G7 code or G6 transmitter and start then move it through
  `start_pending`, `warmup` and `active` on timers. `cgm set` sets fields (`--manual` turns the timers off,
  `--warmup-seconds 60` shortens one, `--worn-g7-code 4321` makes a pairing with any other code never find the sensor,
  `--refuse g7_code` makes the pump refuse the app's code, `--egv 65`). `cgm advance [PHASE] [--age-hours H]` moves it
  on: the next phase, or `active`, `grace`, `signal_loss`, `expired`, `failed`, `not_found` and the rest, so screens
  for a sensor about to expire, in its grace period, or failed can be reached without waiting days.
- `./trio-test-harness faketandem api METHOD PATH ['JSON']`: anything else, done the way the pump does it on its own
  (see faketandem's README). Some examples:
  - `POST /api/state/bolus/start '{"units": 1.5}'`: a bolus from the pump's screen.
  - `POST /api/state/tempbasal/start '{"percent": 150, "duration_minutes": 30}'`: a temp rate set on the pump.
  - `POST /api/state/suspend` and `POST /api/state/resume`.
  - `GET /api/log`: the pump's record of every message the app sent it.
- What the driver did is in TandemKit's log: `./trio-test-harness --target sim logs tandem --since 10m` (or `--grep`).
  For Trio, `logs trio` shows what Trio made of it.

Two things to know:
- Always start or restart the app with `./trio-test-harness --target sim launch`, never from its icon: the launch
  gives the app faketandem's address. An app started any other way cannot reach the pump.
- The app computes IOB with its own insulin model, so it will not equal the pump's figure. Report both, and what you
  compared.
- faketandem answers at once. To see a progress screen, or a timeout, slow the pump's answers down with a
  `delay_response` fault on the request the screen waits for, and `faketandem api DELETE /api/faults` takes it back.
  For TandemKit's Load Pump History step: the driver reads the pump's whole log right after pairing, so the step's own
  reads are few and a delay on `HistoryLogRequest` never fires. Hold back the status read every history pass starts
  with, after pairing and before tapping Load: `faketandem api POST /api/faults '{"kind": "delay_response", "message":
  "HistoryLogStatusRequest", "delay_ms": 8000, "every": true}'`. Its bar shows how much of the window the log read at
  pairing covers, so it can open near 100%.
