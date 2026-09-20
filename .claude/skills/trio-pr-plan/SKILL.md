---
name: trio-pr-plan
description: Turn a Trio (or Trio submodule) pull request into a concrete, screen-by-screen test plan — read the PR title/description/diff with gh, map changes to Trio screens and flows, and write the plan into the run's pr.md. Use when asked what to test for a PR, to plan a test, or before exercising a PR's changes.
---

# trio-pr-plan

1. Get the PR: `gh pr view <URL> --json title,body,files,baseRefName,headRefName` and, when the description is thin,
   `gh pr diff <URL> | head -400`. For a submodule PR also note which Trio branch pins it (`runs/<run>/pr.md` says).
2. Classify every change as: UI (which screen? Home, Settings ▸ Devices/Therapy/Algorithm/Features/Notifications/Services,
   onboarding, treatments sheet, history, adjustments), pump/CGM driver (which device? can the fake devices reach it?),
   algorithm/oref (observable via Settings ▸ Algorithm and the home forecast/IOB/COB numbers), or non-UI (build/tests only).
3. Write the plan as numbered checks with expected results into `runs/<run>/pr.md` under "Test plan", each mapped to
   `th` actions (tap-text labels, values to type, what to screenshot). Include a negative check where the PR fixes a bug.
4. State what cannot be tested with fake devices (real pump/CGM protocol paths, Nightscout, HealthKit) so the report is honest.
Trio facts that matter: only the device picker has accessibility identifiers (`devicePickerItem-<id>`); everything else is
matched by visible label; the home has tabs Main/History/Add Treatment/Adjustments/Settings; simulator = iOS 26 UI, phone = iOS 18 UI.
Keep patient-safety wording precise: the report describes app behaviour, not dosing advice.
