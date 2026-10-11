You are running non-interactively from the trio-test-harness web UI (`th webui`): nobody can answer questions, so never ask any.

`trio-test-e2e --app loop` has already prepared run `{run_dir}` on the jailbroken test iPhone (target `rooted-vnc`,
driven over TrollVNC): Loop, built from LoopWorkspace, is installed, launched and on its home screen{smoke_note}.
TH_APP=loop is set, so every `./trio-test-harness` command acts on Loop. Follow the trio-test-e2e skill from step 3:

1. Read `{run_dir}/pr.md` (PR title, description, submodule info) and turn it into concrete checks (trio-pr-plan skill).
   For every setting or screen involved, look up where it is in LoopDocs first: `./trio-test-harness docs search "..."`.
2. Exercise them with the trio-ui-drive skill using `./trio-test-harness --target rooted-vnc ...` (or
   `./bin/th --target rooted-vnc ...`), screenshotting every meaningful state into this run (`shot --name ... --note ...`)
   and recording observations with `./trio-test-harness run note "..."`. Look at each screenshot before tapping;
   coordinates are screenshot pixels (the phone's native resolution). Loop writes no text log of its own; TandemKit's
   is there: `./trio-test-harness --target rooted-vnc logs tandem --since 5m` (or `--grep`; see your limits).
3. Finish with `./trio-test-harness run finish passed|failed|partial --summary "..."` and `./trio-test-harness report html`.

This run is `$TH_RUN`, so `shot`, `run note` and `run finish` go to it without `--run`.

Finding your way around Loop:
- Home is the status screen: the HUD at the top (glucose, loop status, pump), the charts (Glucose, Active Insulin,
  Insulin Delivery, Active Carbohydrates), and a toolbar of five icons along the bottom, left to right: Add Meal,
  Pre-Meal Targets, Bolus, Workout Targets, Settings. The phone has no accessibility tree, so tap the icons by position.
- Settings (the rightmost icon) holds Closed Loop, the devices, Therapy Settings and the rest; "Done" closes it.
  Tapping the pump or CGM in the HUD opens that device's own settings.
- Loop runs open loop (Closed Loop off) on LoopOnboarding's mock therapy settings: correction range 105–110 mg/dL,
  maximum basal 6 U/h, maximum bolus 8 U, glucose safety limit 75 mg/dL, insulin sensitivity 50 mg/dL/U, carb ratio
  15 g/U, basal 1.2 U/h. Turn Closed Loop on only when the PR is about automatic dosing.
- A CGM Simulator added on the phone has no glucose model: when the PR needs readings, set one under the CGM's
  settings ▸ Simulator Settings ▸ Model (Constant asks for one value).

It is a real phone, so the Lock Screen, Live Activities and the Dynamic Island are all real:
- Lock Screen: `./trio-test-harness device lock`, then `./trio-test-harness --target rooted-vnc shot`;
  `./trio-test-harness device unlock` afterwards (the harness knows the passcode; never type it yourself).
- A dark screen means the phone locked itself: `./trio-test-harness device unlock`.

Rules:
- Only Loop's CGM Simulator and Pump Simulator, and made-up values. No real health data, device identifiers or tokens.
- Stay inside Loop: do not change iOS settings, uninstall apps, or pair real Bluetooth devices.
- Do not publish Artifacts, post PR comments, push, or change the LoopWorkspace checkout; the web UI handles reporting.
- If part of the PR truly needs hardware (BLE pairing, a physical pump/sensor), say so and record what you could check.
- Stay within the time limit; prefer a few well-evidenced checks over many shallow ones.

Your final message is shown in the web UI as "Agent findings": markdown with the verdict, what you exercised
(with screenshot names), what you could not, and any bugs or regressions found.
{extra}
