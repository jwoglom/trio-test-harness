---
name: trio-test
description: Chained end-to-end Trio test workflow — checkout (Trio or submodule PR/branch) → build → install → launch/onboarding → fake CGM & pump → PR-driven exploratory testing → report artifact → optional PR comment — on the iOS Simulator and/or the USB iPhone. Use when asked to test, verify, smoke test, or screenshot Trio / a Trio PR / a TandemKit or other submodule change; each stage is also its own skill.
---

# trio-test — the chain

Fastest route: the `trio-test-e2e` skill (`./trio-test-e2e --mode simulator|rooted-vnc|iphone-usb --pr <URL>`) runs stages 1–6 in one go and
prints where the run is. Use the per-stage skills when the user wants only one part, when the script stops, or to redo a stage.

| # | Stage | Skill | Command |
|---|---|---|---|
| 0 | Environment | `trio-sim-setup` / `trio-phone-setup` | `th doctor`, `th sim boot`, `th wda start`, `th --target rooted-vnc device unlock`, daemons |
| 1 | Checkout | `trio-checkout` | `th build --pr <URL> --checkout-only` (Trio or submodule PR; `--trio-branch` if needed) |
| 2 | Build | `trio-build` | `th --target sim build --pr <URL>` / `th --target rooted-vnc build --pr <URL> --trollstore` |
| 3 | Install | `trio-install` | `th --target sim install --reinstall` / `th --target rooted-vnc install --appsync` |
| 4 | Launch + home | `trio-launch` | `th --target … launch --preseed-onboarding` or `flow onboarding-walk`; `th flow at-home` |
| 5 | Fake devices | `trio-fake-devices` | `th flow add-cgm-sim`, `th flow add-pump-sim` |
| 6 | Plan | `trio-pr-plan` | read `runs/<run>/pr.md`, `gh pr view/diff`, write numbered checks |
| 7 | Exercise | `trio-ui-drive` | `th shot/tap-text/type/find/tree…`, screenshot every state into the run |
| 8 | Report | `trio-report` | `th run finish …`, `th report html` → Artifact; ask, then `th report pr <URL> --artifact-url <link>` |

Rules: one run per target (`th --target … run new <name>`); both targets requested → simulator first. Builds are slow
(30–60 min) and the simulator must be shut down while a build runs. Fake data only in screenshots; no UDIDs/passcodes/tokens
anywhere. Comment on a PR only after the user agrees; edit its description only if asked. A build failure caused by the PR
is itself the result. Look at every screenshot before the next action.
