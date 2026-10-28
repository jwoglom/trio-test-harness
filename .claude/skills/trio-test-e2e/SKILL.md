---
name: trio-test-e2e
description: One-shot end-to-end Trio test by running `trio-test-e2e` (checkout a Trio or submodule PR/branch → build → install → launch → home screen → fake CGM/pump → HTML report), then exercising the PR's changes and offering a PR comment. Use when the user says "run trio-test-e2e", "test this PR end to end", "e2e test Trio on the simulator/phone", or gives a PR link and a target and wants the whole thing done.
---

# trio-test-e2e — invoke `trio-test-e2e`

The deterministic stages live in one script; you run it, then do the exploratory part.
Look up every setting or screen you need in the Trio docs first (`./trio-test-harness docs search "..."`, `docs page ...`):
they say where it is in the app and what it does.
**Is the device free?** `th busy` (the device lock) is the only answer: it names the run holding it, or says idle. Never judge from process lists; an idle `claude ./trio-test-e2e ...` session is not a run. The script holds the lock for its run (and waits for it).

1. Build the command from the request (ask only for what is missing; the script also prompts on a TTY):
   ```
   cd ~/repos/trio-test-harness
   ./trio-test-e2e --mode simulator|rooted-vnc|iphone-usb --pr <Trio or submodule PR URL> [--trio-branch feat/tandem] [--cherry-pick <PR|sha|A..B|branch>] [--onboarding walk] [--yes]
   ./trio-test-e2e --mode simulator --branch dev            # a branch instead of a PR
   ./trio-test-e2e --mode simulator --pr <PR> --watch      # + the app's Watch app on a paired watch simulator (--target watch)
   ./trio-test-e2e --mode rooted-vnc --skip-build --skip-install   # reuse the last build
   ./trio-test-e2e --app loop --mode simulator --pr <TandemKit, LoopKit/Loop or LoopWorkspace PR>   # Loop instead of Trio
   ./trio-test-e2e --mode simulator --pr <TandemKit PR> --pump faketandem [--pump-setup walk] [--pump-scenario recent-doses]
                                                            # TandemKit against an emulated Mobi (trio-fake-devices skill)
   ```
   `--app loop` tests Loop built from LoopKit/LoopWorkspace (`feat/tandem-closed-beta` has TandemKit) through the same
   stages; the agent hand-off sets `TH_APP=loop`, so every `th` command then acts on Loop and `th docs` reads LoopDocs.
   `rooted-vnc` = the jailbroken iPhone (TrollVNC + SSH), `iphone-usb` = any iPhone through macOS iPhone Mirroring. Inside Claude Code the script never spawns another Claude (it checks
   `CLAUDECODE`); from a plain terminal it hands off to `claude` with this workflow after the smoke test.
   Builds take 30–60 min on this iMac: run it in the background and keep the simulator shut down meanwhile if a
   simulator build is running (`th sim shutdown`); the script boots it when needed.
2. When it finishes it prints `run:`, `report:` and `pr.md:` paths. If it failed, the run's last screenshots and
   `build/xcodebuild-<target>.log` say why; a failing build of the PR itself is a test result — report it.
3. Read `runs/<run>/pr.md` (PR title, description, submodule info). Turn the description into concrete checks
   (`trio-pr-plan` skill) and exercise them with the `trio-ui-drive` skill, screenshotting into the same run
   (`th --target <sim|rooted-vnc|iphone-usb> shot --name … --note …`; `th run note "…"` for observations).
4. `th run finish passed|failed|partial --summary "…"`, `th report html`, publish `report.html` as an Artifact.
5. Ask the user (AskUserQuestion) whether to post the PR comment; if yes:
   `th report pr <PR URL> --artifact-url <artifact link>` (screenshots are attached inline with `gh --attach`; the artifact link goes in the header).
6. Summarize: verdict, commit, what was exercised, what was not, links.
