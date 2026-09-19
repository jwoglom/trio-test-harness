---
name: trio-test
description: End-to-end test of the Trio iOS app (a Trio branch/PR, optionally with a submodule branch such as TandemKit) on the iOS Simulator or the USB-attached iPhone. Builds, installs, drives the UI, saves screenshots, and delivers an HTML artifact and (for PRs) a PR comment. Use when asked to "test", "verify", "smoke test", "screenshot", or "try out" Trio, a Trio PR, or a Trio submodule change on the simulator or the phone.
---

# trio-test — orchestrate a Trio app test

Harness repo: `~/repos/trio-test-harness` (CLI: `~/repos/trio-test-harness/bin/th`). Everything below assumes
`cd ~/repos/trio-test-harness` and `th` = `./bin/th`. Read `README.md` there if anything is unclear.

## 0. Parse the request
Identify: (a) **source** — Trio branch, PR (URL/number), in-place checkout, and any **submodule override**
(`TandemKit@feat/x`, `TandemKit@pr/12`, `TandemKit=https://github.com/fork/TandemKit#branch`);
(b) **target** — `sim` (default) or `device` (the user's iPhone, driven over TrollVNC); or both;
(c) **what to exercise** — default is the smoke test (launch → home screen with Glucose Simulator + Pump Simulator);
(d) **where results go** — always an HTML artifact for the user; if a PR was given, also a PR comment (description edit only if asked).

Testing a TandemKit change means Trio on `feat/tandem` (or the branch the user names) plus `--submodule TandemKit@<branch>`.

## 1. Environment check
`th doctor`. Blockers and their fixes:
- No Xcode → the user must run `./bootstrap-xcode.sh` (needs an Apple ID download and sudo; Xcode 26.3 is the newest that runs on this macOS 15 iMac). Stop and report; do everything not needing Xcode first.
- No iOS/watchOS runtime → `th sim install-runtime` and `th sim install-runtime --platform watchOS` (Trio embeds a Watch app; xcodebuild refuses to start without the watchOS runtime).
- Simulator input backend: `th wda start` (WebDriverAgent runner; first call builds it). Screenshots work without it.
- No usbmuxd device → the phone is unplugged/locked/untrusted. Device tests cannot proceed; do the simulator part.
- TrollVNC unreachable → phone must be unlocked with TrollVNC enabled (Settings ▸ TrollVNC). Check `TH_VNC_PASSWORD`.

## 2. Build + install
See the `trio-build` skill for details. Typical:
```
th --target sim build --branch feat/tandem --submodule TandemKit@my-branch     # or --pr 123 / --pr URL
th --target sim install --reinstall
th --target device build --trollstore && th --target device install --trollstore --open-on-device
```
Builds take 15–40 min on this 2017 iMac; run them in the background and keep working. Build errors are printed
(full log in `build/xcodebuild-<target>.log`). If the build fails because of the change under test, that IS a
finding: capture the errors for the report and stop.

## 3. Start a run, get to the home screen
```
th --target sim run new "<short name>" --title "Trio <branch/PR>: <what>"
th wda start                                       # sim only: input + accessibility backend
th --target sim launch --preseed-onboarding        # sim only: skips onboarding by writing the flag into the app container
th --target device launch                          # phone: Home → Spotlight → "Trio"
th shot --name launch --note "after launch"
```
Every `th shot --name X --note "…"` lands in the run and becomes a report card — take one **at each meaningful state**
(before/after each interaction under test, every error/alert, final state). Look at each screenshot (Read the PNG)
before deciding the next tap. Use the `trio-ui-drive` skill for tapping, typing, scrolling and Trio navigation.
If onboarding is showing (fresh install on the phone, or `--preseed-onboarding` omitted), `th flow onboarding-walk`
clicks through it with defaults, choosing "Skip Nightscout Setup". Fake devices: `th flow add-cgm-sim`, `th flow add-pump-sim`.
`th test smoke` does launch → home → add fake CGM + pump → report in one go.

## 4. Exercise the functionality under test
Drive the specific screens the change touches. Assert by looking (screenshots) and, on the simulator, by
`th tree --filter <text>` / `th find <text>`. Record `th run note "<observation>"` for anything notable.
Never enter real personal/health data; only the simulator CGM/pump and made-up values.

## 5. Finish + deliver
```
th run finish passed|failed|partial --summary "<2–4 sentences: what was verified, what broke>"
th report html                     # → runs/<run>/report.html
```
- Publish `report.html` with the Artifact tool (it is self-contained; screenshots are embedded) and give the user the link.
- If a PR was given: `th report pr <PR URL or owner/repo#N> --mode comment` (screenshots are uploaded to the public
  `TH_ARTIFACT_REPO`; the comment links them). Use `--mode description` only when asked to put it in the PR description.
- Summarize in chat: target(s), commit, pass/fail, the 2–3 most important screenshots' findings, and anything left untested.

## Ground rules
- Screenshots may contain only fake data (simulator devices). Do not put device UDIDs, Apple IDs, or tokens into reports or PRs.
- Don't post to a PR unless the user gave a PR (or asked). Comments are the default; description edits on request.
- Don't `git push`/commit in the Trio checkout used for builds; the harness's `build/Trio-src` clone is disposable.
- Both targets requested → do the simulator first (fast feedback), then the phone.
