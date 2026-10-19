---
name: trio-ui-drive
description: Drive and screenshot the Trio app (or any iOS UI) on the iOS Simulator (idb) or the USB iPhone (TrollVNC) with the trio-test-harness `th` primitives — shot, tap, tap-text, swipe, type, tree, find, OCR — plus Trio navigation facts (onboarding steps, Settings ▸ Devices, fake CGM/pump). Use when you need to interact with the running app or reach a specific Trio screen.
---

# trio-ui-drive

`cd ~/repos/trio-test-harness`; `th` = `./trio-test-harness` (short alias `bin/th`). Pick the target with `--target sim|rooted-vnc|iphone-usb|watch` (or `export TH_TARGET=…`): simulator (WDA tree), jailbroken
phone over TrollVNC (tesseract OCR), or any iPhone through iPhone Mirroring (Vision OCR, `th home` = ⌘1, Spotlight = ⌘3).
**All coordinates are screenshot pixels** (same for both targets). Look at every screenshot before acting.
**Is the device free?** `th busy` (the device lock) is the only answer: it names the run holding it, or says idle. Never judge from process lists; an idle `claude ./trio-test-e2e ...` session is not a run.

## Loop
1. `th shot --name <state> --note "<what this shows>"` → prints the PNG path → **Read it**.
2. Decide the action. Prefer `th tap-text "<label>"` (simulator: accessibility tree; phone: OCR on the screenshot).
   Fall back to `th tap X Y` using coordinates from the screenshot. `th find "<text>"` / `th tree --filter "<text>"` show
   candidates with centers. `th shot --ocr` prints the OCR'd text.
3. Repeat. Wait with `th wait-text "<text>" --timeout 30` for slow transitions.

## Primitives
`tap X Y [--duration s]` · `swipe X1 Y1 X2 Y2` · `drag X1 Y1 X2 Y2 [--hold 0.8 --duration 0.8 --settle 0.5]` (long-press then move: SwiftUI drag & drop) · `scroll down|up|left|right [--amount 0.5]` · `type "text"` (focus a field first) ·
`key return|backspace|tab|escape` · `home` · `tap-text "label" [--exact] [--index n]` · `tree [--filter t] [--all]` (sim only) ·
`find "text"` · `wait-text "text"` · `ocr file.png [--find text]` · `logs [trio|tandem|all] [--grep RE -i] [--since 10m] [--level warning] [-n N] [--save]`
(the app's own log files, to confirm what happened behind a screen; see `th logs --help`).
`db tables` · `db schema ZTABLE` · `db query "SELECT …"` · `db exec "UPDATE …"` (sim: the app's Core Data store; exec
terminates the app, backs the store up into the run and relaunches; change rows, do not insert; see `th db --help`).
`oslog [--since 10m] [--grep RE] [--level debug]` (the app process's system log) · `crashes [--save]` (crash reports
since the run started) · `files ls|cat` (the app's Documents/) · `perf [--watch N --save] [--leaks]` (CPU/memory).
Simulator extras: `th sim defaults <key> <value> [--type -bool]` (UserDefaults of the app), `th sim container` (data path),
`th sim openurl <url>`, `th sim erase` (factory reset the harness simulator),
`th lock` / `th unlock` / `th locked` (lock the simulator through WebDriverAgent; `shot` then captures the real Lock Screen).
Phone extras: `th device wake` (power + swipe up), `th device vnc-check`, Spotlight launch via `th launch --target rooted-vnc`.
The phone driver cannot kill or launch by bundle id: use the App Switcher / Spotlight through taps.

## Trio facts (dev / 0.8.x)
- **Where is a setting?** Look it up before searching screens: `th docs search "<name>"` / `th docs page <path>[#section]`
  / `th docs toc` read https://triodocs.org (the official docs) as text, with the menu path, defaults and limits.
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

## Apple Watch (`--target watch`, simulator only)
A watchOS simulator (`TrioHarnessWatch`) paired with the iPhone simulator runs Trio's Watch app; `trio-test-e2e --watch`
sets it all up. By hand: `th watch pair` → `th --target watch install` (the last build's embedded Watch app) →
`th watch agent start` → `th --target watch launch` → then `th launch` (the phone app **after** the Watch app: Trio's
phone session checks for the watch when it starts, and logs `📱 Phone isReachable after activation: true` when it found it).
`th watch status` shows the pair, the agent and the app in front.
- **Watch settings** (apps read them at launch, so the Watch app in front is relaunched; no simulator restart):
  `th watch settings --wrist left|right --crown left|right --text-size <size>`; no options prints them. Sizes: default,
  extra-small … extra-extra-extra-large, accessibility-medium … accessibility-extra-extra-extra-large. Trio reads only
  the wrist (the crown arrow on the bolus confirmation turns clockwise for left, counterclockwise for right); the crown
  side moves the system's page dots; text size scales every label (largest accessibility size: "Bolus" 87×41 → 111×55 px).
- **Watch model** (experimental: switching broke the phone-watch link in testing, see docs/HANDOFF.md): `th watch model`
  lists them; `th watch model "Apple Watch SE 3 (40mm)"` switches (one simulator per model, kept): pairs it, installs the last build's Watch app, moves the agent, relaunches both apps. Trio lays out by
  exact screen size (`WatchSize`: 40, 41, 42, 44, 45, 49 mm); the 46 mm Series 10/11 matches none and gets `.unknown`.
- Same primitives with `--target watch`: `shot`, `tap`, `tap-text`, `find`, `tree`, `swipe`, `home` (pixels: the 46 mm
  watch is 416×496, 2× points). Plus **`th --target watch crown <turns>`**: 1.0 is one full turn of the Digital Crown,
  negative turns back; `--velocity` in turns per second. Input and the tree come from the watch agent
  (`harness/watchagent`, an XCUITest runner like WebDriverAgent); `th watch agent status|stop|build`.
- **Facts (Trio Watch app, verified 2026-10-18 on watchOS 26.2)**: main screen = glucose bubble, IOB (syringe) top left,
  COB (fork) top right, bottom bar: overrides (clock), **Add** (+), temp targets (target). "STALE DATA" and `--` until
  the phone has pushed its state (it does on each loop; a few seconds after returning from a treatment too).
  Add → "Pick Treatment": **Meal**, **Bolus**, and Meal + Bolus (labelled "Food, syringe.fill"). A row only takes a tap
  on its icon or text, not the gap between them: `tap-text "Bolus" --exact` aims at the text. Bolus input: amount
  (`find " U"`), `Remove`/`Add` steppers, crown dials it (a quarter turn went 0 → 1.40 U), **Enact Bolus**. Confirm
  screen: "To confirm, dial crown." — about half a turn fills it (0.05 steps; Settings ▸ Devices ▸ Smart Watch "Confirm
  Bolus Faster" makes it 0.5) and sends the bolus; **Cancel** returns to the main screen.
- What the phone did: `th logs trio --grep "WatchManager"` (`📱 Received bolus request from watch`, `Enacted bolus via
  APS Manager`). The Watch app's own log reaches the phone's `th logs watch` only when the watch flushes it (every 3 min
  or 100 entries).
- Everything on the watch is a simulator and the bolus goes to the Pump Simulator; no real pump is ever involved.

## Tips
- Simulator taps/tree need an input backend: run `th wda start` once per simulator boot (WebDriverAgent, built on first use
  with `th wda build`; `th wda status` / `th wda stop`). idb is used automatically if installed instead.
- On the phone, TrollVNC's right mouse button is Home and middle is Power (`th home`, `th device wake`). Text input types HID
  keys, so tap the text field first. If the screen is black, the phone is asleep: `th device wake`.
- Alerts from iOS (permissions) are ordinary taps: `th tap-text Allow`.
- Screenshots go into the current run (`th run current`); use `--out path.png` for throwaway shots.
- Live Activities render on the simulator. Enable them in Trio (Settings ▸ Live Activity ▸ Enable Live Activity; off by
  default), wait for a glucose update (the Glucose Simulator ticks every few minutes), then `th lock` + `shot` for the
  Lock Screen, or `th home` + `shot` for the Dynamic Island (iPhone 17 / Pro models). `th unlock` to return.
