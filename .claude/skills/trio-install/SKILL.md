---
name: trio-install
description: Install the last Trio build on the iOS Simulator (simctl) or on the jailbroken USB iPhone (AppSync Unified via pymobiledevice3), or reinstall/uninstall it. Use after `trio-build` or when asked to (re)install Trio on the simulator or phone.
---

# trio-install

`cd ~/repos/trio-test-harness`; `build/last-build.json` records what `th build` produced.

- Simulator: `th --target sim install --reinstall` (boots the `TrioHarness` simulator if needed, uninstalls the old copy so
  the app container starts clean). App data is wiped by `--reinstall`; omit it to keep settings.
- Phone: `th --target rooted-vnc install --appsync` — installs `build/Trio.ipa` through installd (AppSync Unified is on the
  phone; the IPA is fake-signed by the build). ~25 s over USB. Needs the phone plugged in and trusted
  (`th device list` shows it). Reinstalling keeps app data and granted permissions.
- Other phones: `--trollstore --open-on-device` (iOS ≤ 17 TrollStore) or signed builds via devicectl (`TH_DEVELOPER_TEAM`).
- Verify: sim `th sim container` prints the app data path; phone `th device ssh "uiopen --list | grep -i trio"` or
  `th device apps`.
