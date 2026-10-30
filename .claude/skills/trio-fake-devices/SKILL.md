---
name: trio-fake-devices
description: Configure Trio's fake devices — the Glucose Simulator CGM and the Pump Simulator, or a Tandem Mobi emulated by faketandem — on the simulator or the phone via the home-screen "Add CGM" / "Add pump" shortcuts, and verify readings appear. Use when asked to add a fake CGM/pump, set up simulators in Trio, test the Tandem pump's setup or driver (TandemKit) without a pump, or when a test needs glucose data and a pump.
---

# trio-fake-devices

`cd ~/repos/trio-test-harness`; Trio must be on its home screen (see `trio-launch`).

Automated: `th --target <sim|rooted-vnc|iphone-usb> flow add-cgm-sim` then `th --target <sim|rooted-vnc|iphone-usb> flow add-pump-sim`
(both use the home shortcuts when no device is configured, else Settings ▸ Devices). Then
`th shot --name home-with-devices`: expect a glucose bubble (e.g. 77–120 mg/dL, wave pattern) and "200 U / 100 %".

Manual (what the flows do):
1. Tap the **CGM icon** above "Add CGM" (icons, not labels: sim ≈ (603,406) px on iPhone 17; phone ≈ (414,242)).
2. In the "Add CGM" sheet pick **Glucose Simulator** (section Simulator, at the bottom): sim `th tap-text "Glucose Simulator" --exact`
   scrolls to it via WDA; phone: swipe up inside the list (`th swipe 414 1500 414 400`) twice, then `th tap-text`.
   Never swipe on the sheet header — that dismisses it. Close the simulator settings sheet with **Close**.
3. Tap the **pump icon** above "Add pump" → **Pump Simulator** → status page → **Done**.
Danger: the CGM settings sheet ends with a red **Delete CGM**; a stray tap there needs **Cancel**.
Tuning: Glucose Simulator sheet (tap the glucose bubble) has Center Value, Amplitude, Noise, stale values, scenarios;
on the sim the same keys are UserDefaults (`th sim defaults GlucoseSimulator_CenterValue 150`).

Loop (`th --app loop flow add-cgm-sim` / `add-pump-sim`): the HUD at the top shows "Add CGM" / "Add Pump"; each opens
an action sheet of that title, where **CGM Simulator** / **Pump Simulator** are set up at once, with no screens (on a
retry the flows go through Settings, the toolbar's last icon, and close it with **Done**). A new CGM Simulator has no
glucose model, so no readings: on the simulator `trio-test-e2e` writes a sine curve (110 ± 20 mg/dL) into its state and
relaunches Loop; by hand, tap the glucose HUD ▸ Simulator Settings ▸ Model (Constant asks for one value).

## Tandem pump on faketandem (simulator)

For TandemKit itself (pairing, its setup and settings screens, what the driver reads from and sends to the pump),
the pump can be a Tandem Mobi emulated by faketandem instead of the Pump Simulator. The app has to be a virtual-pump
build: `trio-test-e2e --mode simulator --pump faketandem [--pump-setup preseed|walk] [--pump-scenario recent-doses]
[--faketandem-ref REF]` builds Trio with `TANDEMKIT_VIRTUAL_PUMP` (Debug only; its own DerivedData, `…-virtualpump`),
starts faketandem (built with Go from jwoglom/faketandem, `TH_FAKETANDEM_*`) and hands the app its address at every
launch. CI: `pump`, `pump_setup`, `pump_scenario` and `faketandem_ref`; `trio-test-ci --pump faketandem …`.
- `--pump-setup preseed` (default): the app starts with the pump paired. faketandem holds a long-term key, and the
  app's `PumpManagerState.plist` holds the driver's state after a pairing with it. Check with `th faketandem status`
  (connected, authenticated).
- `--pump-setup walk`: the pump is left for you to add. Trio: Home ▸ Add pump (or Settings ▸ Devices) ▸ **Tandem**,
  then TandemKit's setup (insulin type, sounds, pairing). faketandem waits at the double-tap step (`PairStep1`). When
  the app asks for the double tap, run `th faketandem pairing PairStep2`, and the app asks for the code, **123456**.
  With TandemKit#609, a **Load Pump History** step follows pairing.
- `--pump-scenario recent-doses`: the pump has 6 hours of use before the app connects. That is boluses from its screen
  and from an app (the last one stopped at 1.2 of 2 U), a 0% temp, a suspend and resume, a 150% temp still running,
  and about 3.2 U IOB on the pump. The other presets (README "Tandem pump on faketandem" describes each):
  `scheduled-basal` (basal alone), `pump-boluses` (pump-screen boluses only), `app-doses` (another app's boluses and
  temps, one still running), `suspended-now` (suspended when the app connects), `stopped-boluses` (the last bolus
  stopped at 0 U), `control-iq` (Control-IQ on; walk setup), `pump-profile-differs` (the pump's profile is not the
  app's) and `day-of-use` (24 hours). They write a BasalDelivery record every 5 minutes at the rate in force. A JSON
  file works too (faketandem's README, "A pump already in use").
- `th faketandem status` shows what the pump holds. `history` lists its dose records. `pairing [STATE]` reads or sets
  the pairing step. `api METHOD PATH ['JSON']` makes the pump act on its own, for example
  `POST /api/state/bolus/start '{"units":1}'`. `stop --save` keeps its state, history and logs in the run. These work
  from the sandboxed CI agent, through the sim daemon.
- By hand: `th build --pr …` cannot make a virtual-pump build, so use `trio-test-e2e --pump faketandem`. Then
  `th faketandem start --setup preseed [--scenario …] --seed` and `th launch`.
- Restart the app only with `th launch`. Started from its icon, it has no faketandem address and reports Bluetooth as
  unsupported.
