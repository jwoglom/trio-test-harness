# The iPhone: setup and control

Device (as of 2026-09-19): the user's test iPhone, iOS 18.6.1, **rootless jailbreak**, TrollVNC 3.2-272 installed
(Settings ▸ TrollVNC). Framebuffer 828×1792. The Mac and phone are on the same Wi-Fi; the phone also connects over USB
(usbmuxd sees it, and the user keeps `iproxy 2222:22` for SSH).

## Control: TrollVNC (works, verified)
- Configure `.env` (gitignored) with `TH_VNC_HOST=<phone LAN IP>` (shown in Settings ▸ TrollVNC) and `TH_VNC_PASSWORD=<full-access password>`.
  Or leave host empty/localhost with `TH_VNC_VIA_USB=1` to tunnel port 5901 over USB with `iproxy`.
- `th device vnc-check` grabs a frame. `th --target device shot|tap|swipe|type|home` drive it. Right mouse button = Home,
  middle = Power (TrollVNC mapping). `th device wake` = power + swipe up when the screen is black.
- Implementation: `harness/rfb.py`, our own minimal RFB 3.8 client (VNC auth, raw encoding, pointer/key events).
  vncdotool's Twisted client authenticated fine but never received a framebuffer from TrollVNC; do not go back to it.
- The phone shows a "VNC client connected" banner on connect; harmless.
- Text entry: tap the field first, then `th type`. Keys go through as HID events.
- No accessibility tree over VNC. `th tap-text` / `th find` fall back to OCR (`harness/ocr.py`, tesseract, handles
  white-on-colour buttons via an inverted pass). Verified on synthetic iPhone-sized screenshots; validate on real Trio screens.

## Installing apps: AppSync Unified (planned, not yet done)
TrollStore does **not** exist for iOS 18, so the `--trollstore` install path in the harness does not apply to this phone.
The user's chosen route: install **AppSync Unified** (https://github.com/akemin-dayo/AppSync, supports iOS 18) via the
jailbreak's package manager, driven over VNC. Then unsigned/ad-hoc IPAs install through the normal installd service.

Steps for the agent (all over VNC, screenshot after each step):
1. `th --target device home`, find the package manager (Sileo or Zebra; swipe pages / use Spotlight: `th launch --target device --app-name Sileo`).
2. Sources ▸ Add: `https://cydia.akemi.ai/` (Karen's repo — hosts AppSync Unified). Refresh.
3. Search "AppSync Unified" ▸ Install ▸ confirm ▸ respring if prompted.
4. From the Mac, install an IPA over USB (no signing needed once AppSync is active):
   ```
   th --target device build --trollstore        # builds an ad-hoc signed Trio.ipa (name kept for compatibility)
   th --target device install --appsync         # = pymobiledevice3 apps install build/Trio.ipa
   ```
   `--appsync` uses `pymobiledevice3 apps install`, which needs the phone paired/trusted over USB.
5. Launch: `th launch --target device` (Home ▸ Spotlight ▸ "Trio" ▸ return). Trio's first launch shows onboarding;
   `th flow onboarding-walk` clicks through with defaults, or drive it with `th shot` / `th tap-text`.
Alternative if AppSync is unwanted: sign with an Apple ID in Xcode (`TH_DEVELOPER_TEAM`, Developer Mode on the phone) and
`th --target device build && th --target device install` (devicectl).

Caveats to check on first use: Trio needs Bluetooth, HealthKit, App Group and Notification entitlements; ad-hoc signing
embeds them, but confirm the app actually launches and that the App Group container resolves (Trio's onboarding and settings
storage depend on it). If the app crashes at launch, look at `pymobiledevice3 syslog live` filtered on "Trio".

## Other useful device commands
- `th device list` — usbmuxd + devicectl view. `pymobiledevice3 usbmux list` for raw details.
- Screenshots via VNC are the reliable path; `pymobiledevice3 developer dvt screenshot` needs the developer disk image / tunnel.
- The phone is the user's personal test device: don't change settings beyond what a test needs, and don't uninstall other apps.
