You are running in GitHub Actions, non-interactively: nobody can answer questions, so never ask any.

`trio-test-e2e --app loop` has already prepared run `{run_dir}` on the iOS Simulator (target `sim`): Loop, built from
LoopWorkspace, is installed, launched and on its home screen{smoke_note}. TH_APP=loop is set, so every
`./trio-test-harness` command acts on Loop. Follow the trio-test-e2e skill from step 3:

1. Read `{run_dir}/pr.md` (PR title, description, submodule info) and turn it into concrete checks (trio-pr-plan skill).
   For every setting or screen involved, look up where it is in LoopDocs first: `./trio-test-harness docs search "..."`.
2. Exercise them with the trio-ui-drive skill using `./trio-test-harness --target sim ...` (or `./bin/th --target sim ...`),
   screenshotting every meaningful state into this run (`shot --name ... --note ...`) and recording observations
   with `./trio-test-harness run note "..."`. Look at each screenshot before tapping. Loop writes no text log of its
   own; TandemKit's is there: `./trio-test-harness --target sim logs tandem --since 5m` (or `--grep`; see your limits).
3. Finish with `./trio-test-harness run finish passed|failed|partial --summary "..."` and `./trio-test-harness report html`.

Finding your way around Loop:
- Home is the status screen: the HUD at the top (glucose, loop status, pump), the charts, and a toolbar of icons whose
  labels only `./trio-test-harness --target sim tree` shows: Add Meal, Pre-Meal Targets, Bolus, Workout Targets, Settings.
- Settings (the last icon) holds Closed Loop, the devices, Therapy Settings and the rest; "Done" closes it. Tapping the
  pump or CGM in the HUD opens that device's own settings.
- Loop runs open loop (Closed Loop off) on LoopOnboarding's mock therapy settings: correction range 105–110 mg/dL,
  maximum basal 6 U/h, maximum bolus 8 U, glucose safety limit 75 mg/dL, insulin sensitivity 50 mg/dL/U, carb ratio
  15 g/U, basal 1.2 U/h. Turn Closed Loop on only when the PR is about automatic dosing.
- The CGM Simulator added by the smoke stage reads a sine curve around 110 mg/dL, one reading every 5 minutes (`sleep`
  while waiting); its model is under its own settings ▸ Simulator Settings.
- Lock Screen: `./trio-test-harness --target sim lock`, then `shot`; `./trio-test-harness --target sim unlock` afterwards.
- Face ID: Loop asks for it before a bolus. The simulator has a face enrolled. Once the Face ID prompt shows, answer it
  with `./trio-test-harness faceid match`, or with `faceid nomatch` to test a refused one.
Only genuinely hardware-bound behavior (Bluetooth pumps and sensors, real pairing) is out of reach.

Rules:
- Only Loop's CGM Simulator and Pump Simulator, and made-up values. No real health data, device identifiers or tokens.
- Do not publish Artifacts, post PR comments, push, or change the LoopWorkspace checkout; the workflow handles reporting.
- If part of the PR truly needs hardware (BLE pairing, a physical pump/sensor), say so and record what you could check.
- Stay within the time limit; prefer a few well-evidenced checks over many shallow ones.
- Never shut down, reboot or erase the simulator: from your sandbox its display does not come back, and nothing
  after that can be driven. If WebDriverAgent stops answering, run `./trio-test-harness wda stop` and `wda start`,
  then relaunch the app with `./trio-test-harness --target sim launch`.

Your final message becomes the job summary's "Agent findings": markdown with the verdict, what you exercised
(with screenshot names), what you could not, and any bugs or regressions found.
{extra}
