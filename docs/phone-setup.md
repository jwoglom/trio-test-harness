# The iPhone: setup and control

Two ways to drive a physical iPhone:
- **`rooted-vnc`** — the jailbroken test phone: TrollVNC for screen/input, SSH for launch/diagnostics/pre-seeding, AppSync for installs. Everything below except the last section is about this mode.
- **`iphone-usb`** — any iPhone through macOS **iPhone Mirroring**, no root (see the last section).

Device (as of 2026-09-19): the user's test iPhone, iOS 18.6.1, **rootless jailbreak**, TrollVNC 3.2-272 installed
(Settings ▸ TrollVNC). Framebuffer 828×1792. The Mac and phone are on the same Wi-Fi; the phone also connects over USB
(usbmuxd sees it, and the user keeps `iproxy 2222:22` for SSH).

## Control: TrollVNC (works, verified)
- Configure `.env` (gitignored) with `TH_VNC_HOST=<phone LAN IP>` (shown in Settings ▸ TrollVNC) and `TH_VNC_PASSWORD=<full-access password>`.
  Or leave host empty/localhost with `TH_VNC_VIA_USB=1` to tunnel port 5901 over USB with `iproxy`.
- `th device vnc-check` grabs a frame. `th --target rooted-vnc shot|tap|swipe|type|home` drive it. Right mouse button = Home,
  middle = Power (TrollVNC mapping). `th device wake` = power + swipe up when the screen is black.
  `th device lock` presses power (lock the phone to screenshot the Lock Screen and Live Activities); `th device unlock` brings it back.
- Implementation: `harness/rfb.py`, our own minimal RFB 3.8 client (VNC auth, zlib with raw fallback, pointer/key events).
  zlib (2026-09-25): 259 KB instead of 5.8 MB per frame, ~165 ms instead of ~230 ms; `TH_VNC_RAW=1` forces raw.
  The phone needs ~140 ms to produce any frame, so compression can't go much lower.
  vncdotool's Twisted client authenticated fine but never received a framebuffer from TrollVNC; do not go back to it.
- The phone shows a "VNC client connected" banner on connect; harmless.
- Text entry: tap the field first, then `th type`. Keys go through as HID events.
- No accessibility tree over VNC. `th tap-text` / `th find` fall back to OCR (`harness/ocr.py`, tesseract, handles
  white-on-colour buttons via an inverted pass). Verified on synthetic iPhone-sized screenshots; validate on real Trio screens.

## Installing on a non-jailbroken iPhone: Xcode (added 2026-10-06)
For any iPhone with Developer Mode (paired with this Mac once in Xcode or Finder), install the way Xcode's Run does — a build
signed with your team, installed with `devicectl`:
```
th build --in-place ~/repos/Trio --xcode --no-fetch   # signed with DEVELOPER_TEAM from the checkout's ConfigOverride.xcconfig
th install --xcode                                    # xcrun devicectl device install app → the only paired iPhone on USB
th install --xcode --device <udid|name>               # pick one when several are reachable (or set TH_XCODE_DEVICE)
```
- The build targets `generic/platform=iOS`, so the phone need not be connected or unlocked while building. (Building
  straight to the device, `-destination id=<udid>`, waits for its developer disk image and times out on a locked phone.)
- The install mounts the developer disk image first, which fails while the phone is locked
  (`kAMDMobileImageMounterDeviceLocked`); the harness then says to unlock it. Auto-Lock ▸ Never keeps it unlocked while testing.
- Verified 2026-10-06 on an iPhone SE (iOS 18.7.8, USB): `th build --xcode` of nightscout/Trio `main` (team from
  `TH_DEVELOPER_TEAM`), then `th install --xcode` auto-picked the phone and installed in ~17 s.
- This installs only; these phones have no VNC/SSH, so driving the app needs `iphone-usb` (iPhone Mirroring) or a person.

## Installing apps: AppSync Unified (installed 2026-09-20)
TrollStore does **not** exist for iOS 18, so the `--trollstore` install path does not apply to this phone. AppSync Unified
116.0 (https://github.com/akemin-dayo/AppSync) is now installed, so unsigned/ad-hoc IPAs install through installd:
```
th --target rooted-vnc build --trollstore        # ad-hoc signed arm64 Trio.ipa (flag name kept; it just means "no Apple signing")
th --target rooted-vnc install --appsync         # pymobiledevice3 apps install build/Trio.ipa (phone on USB, trusted)
th launch --target rooted-vnc                    # uiopen --bundleid <id> over SSH (falls back to Spotlight over VNC)
```
How it was installed (for the record / if it ever needs redoing): Karen's repo (https://cydia.akemi.ai/) was returning
Cloudflare 522, so the deb came from the GitHub release. Its `Depends: firmware (<= 18.2)` blocked iOS 18.6.1 in Sileo
("Depends firmware"), so the control file was repacked with `firmware (<= 18.9)` (`ar x` / edit / `tar czf` / `ar rc`; the tweak
itself declares iOS 18 support). Delivery to the phone: the phone (192.168.14.x) **cannot reach the Mac's subnet**
(192.168.4.x), so `uiopen http://<mac-ip>` fails; use an SSH reverse tunnel instead:
`ssh -N -R 8765:127.0.0.1:8765 mobile@<phone>` + `python3 -m http.server 8765` on the Mac, then
`uiopen http://127.0.0.1:8765/file.deb` on the phone → Safari download → Downloads → share sheet → Sileo → GET → Confirm →
Restart SpringBoard. `uiopen file:///...` does not open Sileo, and `dpkg -i` needs root (no passwordless sudo for mobile).

## Lock screen / passcode
The phone has a 6-digit passcode and auto-locks quickly. `th device unlock` wakes it (power), swipes up and types
`TH_DEVICE_PASSCODE` from `.env` (never commit it, never print it). Without the passcode the harness cannot see or drive
anything once the screen locks; alternatively set Settings ▸ Display & Brightness ▸ Auto-Lock to Never on this test phone.
`uiopen` works while locked but the app is not visible until unlocked. Screens that are all black (mean brightness < 8)
mean the display is off.

## Build + signing for the phone (verified 2026-09-20)
Xcode 26 refuses `CODE_SIGN_IDENTITY=-` for the iphoneos SDK ("Ad Hoc code signing is not allowed"), so `th --target rooted-vnc
build --trollstore` builds with `CODE_SIGNING_ALLOWED=NO` and then fake-signs every nested bundle and the app with
`codesign --sign -` plus entitlements derived from `Trio/Resources/Trio.entitlements` (app group, HealthKit, NFC, APNs) and
the identifier entitlements Xcode normally injects (see `harness/build.py: fakesign_app`). The resulting `build/Trio.ipa`
installed through AppSync in ~25 s with `th --target rooted-vnc install --appsync`.

## SSH access (works, key-based, user `mobile`)
`th device ssh <cmd>`, `th device diag` (uptime, VNC process/port, top, disk), `th device vnc-restart` (killall
trollvncserver; it runs as mobile and launchd respawns it), `th device kill Trio`, `th device apps`. `sudo` needs a password,
so anything root-only is off the table unless the user adds a NOPASSWD rule. Tools present on the phone: uiopen, killall,
launchctl, dpkg (no curl, nc, plutil). TrollVNC prefs live at
`/var/jb/var/mobile/Library/Preferences/com.82flex.trollvnc.plist` (binary; edit on the Mac with plutil, scp back, restart).

## Persistent connection: `th daemon`
`th --target rooted-vnc daemon start` keeps one VNC connection open in a background process (socket `/private/tmp/th-<checkout>/rooted-vnc.sock`: `config.daemon_socket`);
every `th` command uses it automatically (2–3 s per screenshot instead of a reconnect each time, and no connect/disconnect
banners on the phone). `th daemon status|stop|restart`. The daemon reconnects once if the phone drops the connection.
Same for the simulator: `th --target sim daemon start` holds the WDA session.

## Other useful device commands
- `th device list` — usbmuxd + devicectl view. `pymobiledevice3 usbmux list` for raw details.
- Screenshots via VNC are the reliable path; `pymobiledevice3 developer dvt screenshot` needs the developer disk image / tunnel.
- The phone is the user's personal test device: don't change settings beyond what a test needs, and don't uninstall other apps.


## `iphone-usb`: iPhone Mirroring (no jailbreak) — added 2026-09-20, not yet paired here
Driver: `harness/mirror.py` on top of [phone-harness](https://github.com/ShawnPana/phone-harness) (MIT, `vendor/phone-harness`,
pip-installed into `.venv`): `screencapture -l <window>` for frames, CGEvent HID taps/drags/keys, Apple Vision OCR for text.
Shortcuts inside the mirror: ⌘1 Home, ⌘2 App Switcher, ⌘3 Spotlight (used by `th --target iphone-usb launch`).
Requirements (user's hands): macOS 15+, iOS 18+, **same Apple ID on Mac and phone**, Bluetooth + Wi-Fi on both, phone near
the Mac and **locked**, iPhone Mirroring paired once (open it → Continue → confirm on the phone → enter the Mac password).
The terminal (iTerm) needs Accessibility + Screen Recording. Check with `th device mirror-status` and `th --target iphone-usb shot`.
Status on 2026-09-20: iPhone Mirroring on this iMac looks for "James Woglom's iPhone" (the phone on the user's Apple ID),
not the jailbroken test phone ("HackiPhone"), and reported "Unable to Connect" — the test phone would need to be signed into
the same Apple ID (or another iPhone used). Installing Trio in this mode needs a signed build (`TH_DEVELOPER_TEAM`, Apple ID in
Xcode, Developer Mode on the phone) and `devicectl`; onboarding cannot be pre-seeded, so `th flow onboarding-walk` runs (Vision OCR).
Limits (from phone-harness): unlocking the phone pauses mirroring; OCR sees text not icons; no multi-touch.
