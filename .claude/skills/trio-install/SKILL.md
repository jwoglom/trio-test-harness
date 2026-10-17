---
name: trio-install
description: Install the last Trio build on the iOS Simulator (simctl), on the jailbroken USB iPhone (AppSync Unified via pymobiledevice3), or on any non-jailbroken iPhone the Xcode way (team-signed build + devicectl), or reinstall/uninstall it. Use after `trio-build` or when asked to (re)install Trio on the simulator or a phone.
---

# trio-install

`cd ~/repos/trio-test-harness`; `build/last-build.json` records what `th build` produced.

- Simulator: `th --target sim install --reinstall` (boots the `TrioHarness` simulator if needed, uninstalls the old copy so
  the app container starts clean). App data is wiped by `--reinstall`; omit it to keep settings.
- Phone: `th --target rooted-vnc install --appsync` — installs `build/Trio.ipa` through installd (AppSync Unified is on the
  phone; the IPA is fake-signed by the build). ~25 s over USB. Needs the phone plugged in and trusted
  (`th device list` shows it). Reinstalling keeps app data and granted permissions.
- Any other iPhone (not jailbroken) — **Xcode**: `th build --xcode` then `th install --xcode [--device <udid|name>]`. The build
  is signed with the user's team (see `trio-build`); `xcrun devicectl device install app` puts it on the phone, like Xcode's
  Run. Device: `--device`, else `TH_XCODE_DEVICE`, else the only paired iPhone on USB (`th device list` shows devicectl's
  view; with several, it lists them and asks). Needs Developer Mode on and the phone **unlocked** — devicectl mounts the
  developer disk image first, and a locked phone fails with "is locked; unlock it and retry". Ask the user to unlock it (no
  VNC on these phones); Settings ▸ Display & Brightness ▸ Auto-Lock ▸ Never keeps it unlocked. Reinstalling keeps app data.
- iOS ≤ 17 TrollStore phones: `--trollstore --open-on-device`.
- Verify: sim `th sim container` prints the app data path; phone `th device ssh "uiopen --list | grep -i trio"` or
  `th device apps`; Xcode installs print `App installed: bundleID …` (or `xcrun devicectl device info apps --device <udid>`).
