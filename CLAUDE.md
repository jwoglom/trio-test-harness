# trio-test-harness — agent instructions

This repo is the tooling for building, installing, driving and screenshotting the **Trio** iOS app
(https://github.com/nightscout/Trio, user fork jwoglom/Trio) on an iOS Simulator and on the user's
jailbroken iPhone, and for reporting results (HTML artifact, PR comments). `--app loop` (or `TH_APP=loop`) does the same
for **Loop**, built from LoopKit/LoopWorkspace (`feat/tandem-closed-beta` has TandemKit); `harness/apps.py` holds what
differs between the two apps and `harness/flows/loop.py` Loop's UI flows.

Start here, in order:
1. `docs/HANDOFF.md` — current state, what is verified, what is not, and the next-steps checklist.
2. `README.md` — architecture and CLI overview.
3. `docs/phone-setup.md` — the iPhone: TrollVNC, AppSync Unified, installing/launching IPAs.
4. `.claude/skills/*/SKILL.md` — the workflow skills: `trio-test-e2e` (runs `trio-test-e2e`), `trio-test` (the chain), and one
   skill per stage (sim-setup, phone-setup, checkout, build, install, launch, fake-devices, pr-plan, ui-drive, report).
   `./install.sh` symlinks them into `~/.claude/skills`.

Conventions:
- Everything goes through `./trio-test-e2e` (whole pipeline, `harness/pipeline.py`) and `./trio-test-harness` (every stage and primitive,
  `harness/cli.py`). Add features there, not as ad-hoc scripts. `trio-test-e2e` from a plain terminal hands off to `claude`
  after the smoke stage; inside Claude Code (CLAUDECODE set) it never spawns another Claude.
- Trio's documentation (https://triodocs.org): `./trio-test-harness docs search|toc|page` reads it as text (with
  `--app loop`, LoopDocs). Always look a setting or option up there to find where it lives in the app before navigating
  or documenting it.
- Whether the phone or simulator is in use: `./trio-test-harness busy` (the device lock, `harness/busy.py`) is the only answer. `trio-test-e2e` holds it for its run and a web UI job for its agent step; the OS frees it when they exit. Never infer a run from process lists: idle `claude` sessions started as `claude ./trio-test-e2e --pr ...` keep that text in their command line for days and drive nothing.
- All UI coordinates are **screenshot pixels** for both targets. Look at the PNG before tapping.
- Secrets/machine settings live in `harness.env` and `.env` (both gitignored). Never commit them, never print the VNC password.
- Screenshots may contain only fake data (Trio's Glucose Simulator, Loop's CGM Simulator, the Pump Simulator). No UDIDs, Apple IDs, tokens, or real CGM data in reports or PRs.
- Don't push or commit inside the Trio checkout the harness builds from (`$TRIO_REPO`; on this Mac that is the user's ~/repos/Trio — PR/branch checkouts leave it on a detached HEAD, so `git checkout dev` restores it), nor in Loop's (`$LOOP_REPO`).
- Trio (or Loop) itself is the repo under test; reference its code by path when documenting flows (see the facts in `harness/flows/trio.py` and `harness/flows/loop.py`).
- Run `./trio-test-harness doctor` first on a new machine. Verify flows against real screenshots before trusting them (see HANDOFF "Unverified").
