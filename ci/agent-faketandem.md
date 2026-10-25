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
- faketandem answers at once. To see a progress screen, or a timeout, slow the pump's answers down. For example,
  `faketandem api POST /api/faults '{"kind": "delay_response", "message": "HistoryLogRequest", "delay_ms": 1500,
  "every": true}'` slows the history reads. `faketandem api DELETE /api/faults` takes it back.
