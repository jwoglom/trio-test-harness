---
name: trio-sim-setup
description: Prepare the iOS Simulator side of the Trio harness — check Xcode and the iOS/watchOS runtimes, create/boot the TrioHarness simulator, build/start WebDriverAgent, start the sim daemon, or shut it all down. Use when the simulator, WDA, or `th --target sim` commands need setting up or are misbehaving.
---

# trio-sim-setup

`cd ~/repos/trio-test-harness`
- `th doctor` — Xcode (26.3 at /Applications/Xcode-26.3.app), iOS + watchOS runtimes (Trio's scheme embeds a Watch app;
  `th sim install-runtime [--platform watchOS]`), tools.
- `th sim boot` — creates/boots `TrioHarness` (`TH_SIM_DEVICE`, default iPhone 17, newest iOS runtime). First boot of a
  new runtime can take 5+ min; the iOS 26 simulator is heavy on this Intel iMac — never boot it while xcodebuild runs.
- `th wda build` (once per Xcode/runtime) then `th wda start` (per boot) — WebDriverAgent on :8100 gives taps, typing,
  element search. If it fails with "Timed out waiting for AX loaded notification":
  `xcrun simctl spawn <udid> defaults write com.apple.Accessibility AccessibilityEnabled -bool true` (+ ApplicationAccessibilityEnabled),
  `th sim shutdown && th sim boot`, retry.
- `th --target sim daemon start` — persistent driver; `th daemon status`.
- Watch: `th watch pair` creates `TrioHarnessWatch` (`TH_WATCH_SIM_DEVICE`, default Apple Watch Series 11 (46mm), newest
  watchOS runtime), pairs it with `TrioHarness` and boots both; `th watch agent start` builds (once, ~20 s) and starts the
  watch's input agent; `th watch status`; `th --target watch daemon start` for the sandboxed agent. Booting both while
  xcodebuild runs failed once here with "launchd_sim failed to respond"; booting the watch again on its own worked.
  Cleanup: `th watch agent stop`, `th --target watch daemon stop`.
- Cleanup: `th wda stop`, `th --target sim daemon stop`, `th sim shutdown`; `th sim erase` factory-resets the simulator.
Screenshots are 1206×2622 px (iPhone 17); coordinates in `th` are pixels.
