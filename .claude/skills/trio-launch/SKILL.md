---
name: trio-launch
description: Launch Trio on the simulator or the phone and get it to the home screen — pre-seeding onboarding (and disabling Crashlytics on the phone) or walking the real onboarding flow, unlocking the phone, dismissing the update alert. Use when asked to launch/open/start Trio, skip or walk onboarding, or reach the Trio home screen.
---

# trio-launch

`cd ~/repos/trio-test-harness`. Bundle id comes from `build/last-build.json` (org.nightscout.TRIOSIMTST.trio).

Fast path (skips onboarding):
```
th --target sim launch --preseed-onboarding      # writes Documents/onboardingCompleted.plist into the sim container
th --target rooted-vnc launch --preseed-onboarding   # same over SSH + crashlyticsSharingEnabled=false (Crashlytics crashes on the jailbroken phone), then uiopen
```
Real onboarding (also the best VNC/OCR exercise): launch without `--preseed-onboarding`, then
`th --target <sim|rooted-vnc|iphone-usb> flow onboarding-walk` — one screenshot per step into the current run; it taps
Skip Nightscout Setup → Next… → Allow (notifications) → Allow (Bluetooth, twice on iOS 18) → Get Started.
Phone first: `th --target rooted-vnc device unlock` (needs `TH_DEVICE_PASSCODE` in `.env`; the phone re-locks within
seconds, so run the next command right away) and `th --target rooted-vnc daemon start` once per session.

After launch: `th --target … shot --name launch`, dismiss "Update Available" with `th tap-text OK --exact`, and confirm the
home screen with `th flow at-home` (looks for two of: History, Adjustments, Settings, Add Treatment).
On the phone, skipping onboarding means Trio never asked for Bluetooth: the home shows "Bluetooth Required" until the
permission exists (it does on the test phone now). Relaunch via SSH: `th device kill Trio` then `th --target rooted-vnc launch`.
