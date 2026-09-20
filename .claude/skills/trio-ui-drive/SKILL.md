---
name: trio-ui-drive
description: Drive and screenshot the Trio app (or any iOS UI) on the iOS Simulator (idb) or the USB iPhone (TrollVNC) with the trio-test-harness `th` primitives — shot, tap, tap-text, swipe, type, tree, find, OCR — plus Trio navigation facts (onboarding steps, Settings ▸ Devices, fake CGM/pump). Use when you need to interact with the running app or reach a specific Trio screen.
---

# trio-ui-drive

`cd ~/repos/trio-test-harness`; `th` = `./trio-test-harness` (short alias `bin/th`). Pick the target with `--target sim|rooted-vnc|iphone-usb` (or `export TH_TARGET=…`): simulator (WDA tree), jailbroken
phone over TrollVNC (tesseract OCR), or any iPhone through iPhone Mirroring (Vision OCR, `th home` = ⌘1, Spotlight = ⌘3).
**All coordinates are screenshot pixels** (same for both targets). Look at every screenshot before acting.

## Loop
1. `th shot --name <state> --note "<what this shows>"` → prints the PNG path → **Read it**.
2. Decide the action. Prefer `th tap-text "<label>"` (simulator: accessibility tree; phone: OCR on the screenshot).
   Fall back to `th tap X Y` using coordinates from the screenshot. `th find "<text>"` / `th tree --filter "<text>"` show
   candidates with centers. `th shot --ocr` prints the OCR'd text.
3. Repeat. Wait with `th wait-text "<text>" --timeout 30` for slow transitions.

## Primitives
`tap X Y [--duration s]` · `swipe X1 Y1 X2 Y2` · `scroll down|up|left|right [--amount 0.5]` · `type "text"` (focus a field first) ·
`key return|backspace|tab|escape` · `home` · `tap-text "label" [--exact] [--index n]` · `tree [--filter t] [--all]` (sim only) ·
`find "text"` · `wait-text "text"` · `ocr file.png [--find text]`.
Simulator extras: `th sim defaults <key> <value> [--type -bool]` (UserDefaults of the app), `th sim container` (data path),
`th sim openurl <url>`, `th sim erase` (factory reset the harness simulator).
Phone extras: `th device wake` (power + swipe up), `th device vnc-check`, Spotlight launch via `th launch --target rooted-vnc`.
The phone driver cannot kill or launch by bundle id: use the App Switcher / Spotlight through taps.

## Trio facts (dev / 0.8.x)
- **Onboarding** (fresh install): Welcome → Before you begin → force-close warning → overview → **Nightscout** (tap "Skip
  Nightscout Setup", then Next) → Units & Pump (pump picker excludes the simulator) → glucose target → basal → carb ratio → ISF
  (chapter-complete screens need one more Next) → delivery limits (5 substeps) → algorithm info → autosens → SMB (10 substeps) →
  target behaviour → **Notifications** (tap Next → iOS "Allow") → **Bluetooth** (in-app alert; Allow) → "Get Started".
  `th flow onboarding-walk` automates this with defaults. On the simulator skip it entirely: `th launch --preseed-onboarding`
  (writes `Documents/onboardingCompleted.plist` into the app container).
- **Home**: tab bar Main · History · (+) Add Treatment · Adjustments · Settings (on iOS 26 the labels are accessibility-only
  icons: chart, clock, plus, sliders, gear). Loading view ~2 s after launch; a version-check alert may appear (tap OK).
- **Verified 2026-09-20 (sim)**: launch → "Update Available" alert → `th tap-text OK --exact`; home shows **Add pump** /
  **Add CGM** shortcuts when nothing is configured — tap the *icon* above the label (label taps do nothing); in the picker use
  `th tap-text "Glucose Simulator" --exact` (WDA scrolls to it) — a `scroll` swipe dismisses the sheet; Pump Simulator setup ends
  with **Done**; afterwards the home shows glucose (~77 mg/dL trace) and "200 U / 100 %". Tapping the CGM bubble opens the CGM
  settings sheet whose bottom button is **Delete CGM** — never tap blindly near the bottom of that sheet.
- **Fake devices**: Settings ▸ Devices ▸ **CGM** ▸ Add CGM ▸ section "Simulator" ▸ **Glucose Simulator**
  (acc. id `devicePickerItem-simulator`). Settings ▸ Devices ▸ **Insulin Pump** ▸ Add Pump ▸ **Pump Simulator**
  (`devicePickerItem-MockPumpManager`), then Continue/Done through the mock pump setup. Flows: `th flow add-cgm-sim`,
  `th flow add-pump-sim`, `th flow back-home`. The CGM screen shows a "Bluetooth required" view until Bluetooth is authorized.
- Glucose Simulator tuning lives in Settings ▸ Devices ▸ CGM (center value, amplitude, noise, scenario) and in UserDefaults
  keys `GlucoseSimulator_CenterValue`, `GlucoseSimulator_Amplitude`, `GlucoseSimulator.simulatedScenario` (sim: `th sim defaults`).
- Settings root lists Devices, Therapy, Algorithm, Features, Notifications, Services and shows the branch/version at the bottom.
- Only one `accessibilityIdentifier` exists in Trio (device picker rows); everything else is matched by visible label.

## Tips
- Simulator taps/tree need an input backend: run `th wda start` once per simulator boot (WebDriverAgent, built on first use
  with `th wda build`; `th wda status` / `th wda stop`). idb is used automatically if installed instead.
- On the phone, TrollVNC's right mouse button is Home and middle is Power (`th home`, `th device wake`). Text input types HID
  keys, so tap the text field first. If the screen is black, the phone is asleep: `th device wake`.
- Alerts from iOS (permissions) are ordinary taps: `th tap-text Allow`.
- Screenshots go into the current run (`th run current`); use `--out path.png` for throwaway shots.
