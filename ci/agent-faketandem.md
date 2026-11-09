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
  - `POST /api/state/filltubing/button '{"hold_seconds": 3}'`: a person holding the pump's button to fill the tubing,
    which TandemKit's Fill Tubing step waits for ("Press and hold the button on your pump"). It works once the app
    has put the pump in fill tubing mode (`workflow_mode` in `status --json`); `'{"down": true}'` and
    `'{"down": false}'` press and release it separately, to see the screen while the button is held.
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

### Pump-side events cookbook
Typed shortcuts for the pump's side (each prints what it did; `faketandem api` stays for the rest, and takes
`fill_tubing` for `filltubing`):
- `faketandem status --field basal.temp_active` (dotted path, one value: `connection.connected`, `insulin.reservoir_units`,
  `workflow_mode`); `faketandem history --type BasalDelivery --last 3`; `faketandem log --last 20 --message Bolus`
  (what the app sent; `--count`).
- Wait for something instead of looping: `wait --until "faketandem status --field connection.connected" --matches true
  --timeout 120` or `wait --until "logs tandem --since 2m" --matches "Bolus.*delivered" -i`; it prints the line, exit 1
  on timeout. `key backspace --count 12`, `logs trio tandem --count`.
- Lost bolus answer (pump delivers, app hears nothing): `faketandem fault add drop_response --message InitiateBolusRequest`.
  Refused: `fault add error_response --message InitiateBolusRequest --error-code 3`. Slow: `fault add delay_response
  --message X --delay-ms 8000 --every`. Link cut: `fault add disconnect --message HistoryLogRequest`;
  `fault add reject_quick_pair`; `fault add drop_fragment --index 1 --every`. `fault clear` takes them all back.
- Radio off and on: `fault add radio_off`, then `fault add radio_on`.
- Occlusion with the temp kept (what TandemKit records for a Mobi: the stop leaves the temp set, no TempRateCompleted, and
  it runs again after the resume): `tempbasal start --percent 150 --duration-minutes 60`, then `suspend --reason occlusion
  --keep-temp`, then `resume`. `--keep-temp` takes occlusion or alarm (alone it means occlusion), not user.
- Notifications (`alert|alarm|malfunction|reminder|cgmalert raise|clear NAME_OR_BIT`, name as pumpX2 spells it with the
  category suffix optional, or a bit number; `--message` on alert/alarm). Raise `alert raise LOW_INSULIN`, then look for it in
  TandemKit's notifications row (Trio: the pump's settings, Notifications) and in `logs tandem --since 2m`; `alert clear
  LOW_INSULIN` is the pump's screen acknowledging it. A second raise of one already standing, or clearing one that is not, is a 409.
- A resume the pump refuses: `alarm raise RESUME_PUMP_ALARM`, `suspend`, then resume from the app. The pump answers status 1
  until the alarm is dismissed, which TandemKit's `ResumeWithAlarmAcknowledgement` does and retries once; `alarm clear
  RESUME_PUMP_ALARM` (or the app's dismiss) lets a resume through. Check `log --message Resume`.
- A malfunction: `malfunction raise 9 --fault-id 8305` (bit 9 is MOTOR). The app cannot dismiss it (its
  `DismissNotificationRequest` is answered with status 1); only `malfunction clear 9` removes it. It does not stop delivery.
- Cartridge change end to end: `set cartridge_ready_seconds=0.5 cartridge_detect_seconds=1 max_fill_seconds=30`, `suspend`;
  start the change wizard in the app (it sends EnterChangeCartridgeMode; the pump streams retracting); `cartridge ready` (or
  wait out `cartridge_ready_seconds`) streams readyToChange; `cartridge insert --units 300`; tap on in the app, whose exit
  streams the cartridge check (0-100%, then the reservoir reads 300 U). Fill tubing: the app enters it, then hold the
  button, `faketandem api POST /api/state/filltubing/button '{"hold_seconds": 5}'` (or `{"down": true}` then `{"down": false}`);
  the app exits and the pump answers TUBING_FILLED. Fill cannula: the app's FillCannulaRequest streams filling, then
  CANNULA_FILLED. `faketandem status --field cartridge` shows where it is.
  - Timeout path: `set fill_cannula_stream_lost=true` before the cannula fill; the pump fills but never streams the final state.
  - Resume-at-tubing-fill path: `set cartridge_loaded=true` stages a cartridge already checked; the pump then refuses
    EnterChangeCartridgeMode (as a Mobi did), and the app resumes at the tubing fill (`LoadStatus` says LOAD_CARTRIDGE).
- Temp basal refusals (status 1, nothing started; each is a switch under `refusals`, default on): a temp set while one runs
  (`tempbasal start ...` then the app's SetTempRate; the app stops the running one first, so this shows when it does not),
  a rate above max basal (`set max_basal_milliunits=2000`), under Control-IQ (`set closed_loop_enabled=true`), and a
  percent whose share of the profile rate is under 0.1 U/h. Also `bolus_while_busy` (a bolus runs or delivery is suspended).
  Turn one off to let it through: `set refusals='{"temp_rate_while_running":false}'` (nested JSON, other switches keep their value:
  `temp_rate_under_control_iq`, `temp_rate_below_minimum`, `temp_rate_above_max_basal`, `bolus_while_busy`).
  The statuses are unconfirmed on firmware for all of them (any nonzero status is a refusal to the app); the max-basal
  refusal itself is also unconfirmed, and the nack reason 1 for a refused bolus permission is too.
- Wait for the app to subscribe before acting: `wait --until "faketandem status --field connection.subscribed" --matches
  QUALIFYING_EVENTS --timeout 120` (a notification sent before it subscribes is lost, as on a real link).
- A bolus from the pump: `bolus start --units 1.5`, `bolus stall|resume|abort|complete`.
- Pump clock: `clock --offset-seconds 20` (skew alert), `clock --offset-seconds 90000` (over the driver's 24 h limit:
  dosing is refused), `clock --shift-pump-time -3600` (the pump's screen moved an hour), `clock --zone Europe/Paris`,
  `clock --advance-seconds 600 --advance-ticks 6` (manual clock), `clock --real` undoes it.
- Pump fields: `set reservoir_units=5 battery_percent=10`, `set closed_loop_enabled=true` (Control-IQ on),
  `set controliq_algorithm_rate=2.2 basal_cycle_enabled=true`, `set max_bolus_milliunits=3000`, `set workflow_mode=fill_tubing`,
  `set idp_profiles='[...]'` (a pump profile that differs from the app's; `faketandem scenario pump-profile-differs`
  does it too).
- Pump replaced: `faketandem api GET /api/settings/PumpVersionRequest` shows the answer's fields; PUT it back with another
  serial (names are case-insensitive).
- MTU 23 against a real phone's: `transport --mtu 23` / `--mtu 247`.
