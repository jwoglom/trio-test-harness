You are running in GitHub Actions, non-interactively: nobody can answer questions, so never ask any.

`trio-test-e2e` has already prepared run `{run_dir}` on the iOS Simulator (target `sim`): Trio is built,
installed, launched and on the home screen{smoke_note}. Follow the trio-test-e2e skill from step 3:

1. Read `{run_dir}/pr.md` (PR title, description, submodule info) and turn it into concrete checks (trio-pr-plan skill).
2. Exercise them with the trio-ui-drive skill using `./trio-test-harness --target sim ...` (or `./bin/th --target sim ...`),
   screenshotting every meaningful state into this run (`shot --name ... --note ...`) and recording observations
   with `./trio-test-harness run note "..."`. Look at each screenshot before tapping.
3. Finish with `./trio-test-harness run finish passed|failed|partial --summary "..."` and `./trio-test-harness report html`.

Rules:
- Only Trio's Glucose Simulator and Pump Simulator, and made-up values. No real health data, device identifiers or tokens.
- Do not publish Artifacts, post PR comments, push, or change the Trio checkout; the workflow handles reporting.
- If the PR cannot be exercised on the simulator (hardware-only, BLE), say so and record what you could check.
- Stay within the time limit; prefer a few well-evidenced checks over many shallow ones.

Your final message becomes the job summary's "Agent findings": markdown with the verdict, what you exercised
(with screenshot names), what you could not, and any bugs or regressions found.
{extra}
