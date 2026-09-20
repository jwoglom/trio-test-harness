---
name: trio-phone-setup
description: Prepare and recover the jailbroken test iPhone for the Trio harness — VNC (TrollVNC) connection and daemon, passcode unlock, SSH diagnostics, restarting TrollVNC, AppSync install path, Bluetooth/permissions. Use when `th --target rooted-vnc` commands fail, the phone is locked or dark, VNC hangs, or the phone needs setting up on a new Mac.
---

# trio-phone-setup

Two phone modes: **`rooted-vnc`** (jailbroken test phone: TrollVNC + SSH + AppSync — everything below) and **`iphone-usb`**
(any iPhone through macOS iPhone Mirroring; `th device mirror-status`, `th --target iphone-usb shot|tap|type|home`; needs
same Apple ID on Mac and phone, Bluetooth+Wi-Fi, phone locked, pairing done once, terminal with Accessibility + Screen Recording;
installs need a signed build + devicectl; onboarding must be walked). Details: `docs/phone-setup.md`.

`cd ~/repos/trio-test-harness`; settings in `.env` (`TH_VNC_HOST`, `TH_VNC_PASSWORD`, `TH_DEVICE_PASSCODE`) — never print them.
- Connectivity: `th device list` (usbmuxd), `th device vnc-check` (one VNC frame), `th device diag` (SSH: uptime, TrollVNC
  process, ports, disk). `th device ssh "<cmd>"` runs anything as `mobile` (no passwordless sudo). The phone cannot reach the
  Mac's subnet: phone→Mac transfers need `ssh -R`.
- Daemon: `th --target rooted-vnc daemon start|status|restart` keeps one VNC session (no connect banners, fast commands).
- Locked/dark screen: `th --target rooted-vnc device unlock` (power → long swipe → keypad taps, one fast sequence). The phone
  re-locks within seconds; issue the next command immediately or ask the user to set Auto-Lock to Never.
- VNC wedged (connects but no frames / timeouts): `th device vnc-restart` (killall trollvncserver; launchd respawns; restarts
  the daemon). If SSH is also dead, the user must toggle TrollVNC in Settings ▸ TrollVNC.
- Installing apps: AppSync Unified is installed; `th --target rooted-vnc install --appsync`. Details and the repack trick for its
  firmware dependency: `docs/phone-setup.md`.
- Launch/kill: `th --target rooted-vnc launch` (uiopen), `th device kill Trio`. Crash logs: `th device ssh "ls -t /var/mobile/Library/Logs/CrashReporter | head"`, scp one and read `termination`/`exception`.
- Screen facts: 828×1792 px framebuffer; right mouse = Home, middle = Power; iOS 18 UI (labelled tab bar).
