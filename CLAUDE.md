# trio-test-harness — agent instructions

This repo is the tooling for building, installing, driving and screenshotting the **Trio** iOS app
(https://github.com/nightscout/Trio, user fork jwoglom/Trio) on an iOS Simulator and on the user's
jailbroken iPhone, and for reporting results (HTML artifact, PR comments).

Start here, in order:
1. `docs/HANDOFF.md` — current state, what is verified, what is not, and the next-steps checklist.
2. `README.md` — architecture and CLI overview.
3. `docs/phone-setup.md` — the iPhone: TrollVNC, AppSync Unified, installing/launching IPAs.
4. `.claude/skills/*/SKILL.md` — the workflow skills (trio-test, trio-build, trio-ui-drive, trio-report).
   `./install.sh` symlinks them into `~/.claude/skills`.

Conventions:
- Everything goes through `./bin/th` (Python package `harness/`, venv in `.venv`). Add features there, not as ad-hoc scripts.
- All UI coordinates are **screenshot pixels** for both targets. Look at the PNG before tapping.
- Secrets/machine settings live in `harness.env` and `.env` (both gitignored). Never commit them, never print the VNC password.
- Screenshots may contain only fake data (Trio's Glucose Simulator / Pump Simulator). No UDIDs, Apple IDs, tokens, or real CGM data in reports or PRs.
- Don't push or commit inside the Trio checkouts the harness builds from. `build/Trio-src` is disposable.
- Trio itself is the repo under test; reference its code by path when documenting flows (see the facts in `harness/flows/trio.py`).
- Run `./bin/th doctor` first on a new machine. Verify flows against real screenshots before trusting them (see HANDOFF "Unverified").
