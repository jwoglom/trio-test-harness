You are running non-interactively from the trio-test-harness web UI (`th webui`): nobody can answer questions, so never ask any.

`trio-test-e2e` has already prepared run `{run_dir}` on the jailbroken test iPhone (target `rooted-vnc`, driven over
TrollVNC): Trio is built, installed, launched and on the home screen{smoke_note}. Follow the trio-test-e2e skill from step 3:

1. Read `{run_dir}/pr.md` (PR title, description, submodule info) and turn it into concrete checks (trio-pr-plan skill).
2. Exercise them with the trio-ui-drive skill using `./trio-test-harness --target rooted-vnc ...` (or
   `./bin/th --target rooted-vnc ...`), screenshotting every meaningful state into this run (`shot --name ... --note ...`)
   and recording observations with `./trio-test-harness run note "..."`. Look at each screenshot before tapping;
   coordinates are screenshot pixels (the phone's native resolution). Check what happened behind the UI in the app's
   logs: `./trio-test-harness --target rooted-vnc logs tandem --since 5m` (or `trio`, `--grep`; see your limits).
3. Finish with `./trio-test-harness run finish passed|failed|partial --summary "..."` and `./trio-test-harness report html`.

This run is `$TH_RUN`, so `shot`, `run note` and `run finish` go to it without `--run`.

It is a real phone, so the Lock Screen, Live Activities and the Dynamic Island are all real:
- Live Activities are off by default in Trio: Settings ▸ Live Activity ▸ Enable Live Activity. Trio starts one on the
  next glucose update (the Glucose Simulator produces one every few minutes; `sleep` while waiting).
- Lock Screen: `./trio-test-harness device lock`, then `./trio-test-harness --target rooted-vnc shot`;
  `./trio-test-harness device unlock` afterwards (the harness knows the passcode; never type it yourself).
- A dark screen means the phone locked itself: `./trio-test-harness device unlock`.

Rules:
- Only Trio's Glucose Simulator and Pump Simulator, and made-up values. No real health data, device identifiers or tokens.
- Stay inside Trio: do not change iOS settings, uninstall apps, or pair real Bluetooth devices.
- Do not publish Artifacts, post PR comments, push, or change the Trio checkout; the web UI handles reporting.
- If part of the PR truly needs hardware (BLE pairing, a physical pump/sensor), say so and record what you could check.
- Stay within the time limit; prefer a few well-evidenced checks over many shallow ones.

Your final message is shown in the web UI as "Agent findings": markdown with the verdict, what you exercised
(with screenshot names), what you could not, and any bugs or regressions found.
{extra}
