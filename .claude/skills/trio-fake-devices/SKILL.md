---
name: trio-fake-devices
description: Configure Trio's fake devices — the Glucose Simulator CGM and the Pump Simulator — on the simulator or the phone via the home-screen "Add CGM" / "Add pump" shortcuts, and verify readings appear. Use when asked to add a fake CGM/pump, set up simulators in Trio, or when a test needs glucose data and a pump.
---

# trio-fake-devices

`cd ~/repos/trio-test-harness`; Trio must be on its home screen (see `trio-launch`).

Automated: `th --target <sim|rooted-vnc|iphone-usb> flow add-cgm-sim` then `th --target <sim|rooted-vnc|iphone-usb> flow add-pump-sim`
(both use the home shortcuts when no device is configured, else Settings ▸ Devices). Then
`th shot --name home-with-devices`: expect a glucose bubble (e.g. 77–120 mg/dL, wave pattern) and "200 U / 100 %".

Manual (what the flows do):
1. Tap the **CGM icon** above "Add CGM" (icons, not labels: sim ≈ (603,406) px on iPhone 17; phone ≈ (414,242)).
2. In the "Add CGM" sheet pick **Glucose Simulator** (section Simulator, at the bottom): sim `th tap-text "Glucose Simulator" --exact`
   scrolls to it via WDA; phone: swipe up inside the list (`th swipe 414 1500 414 400`) twice, then `th tap-text`.
   Never swipe on the sheet header — that dismisses it. Close the simulator settings sheet with **Close**.
3. Tap the **pump icon** above "Add pump" → **Pump Simulator** → status page → **Done**.
Danger: the CGM settings sheet ends with a red **Delete CGM**; a stray tap there needs **Cancel**.
Tuning: Glucose Simulator sheet (tap the glucose bubble) has Center Value, Amplitude, Noise, stale values, scenarios;
on the sim the same keys are UserDefaults (`th sim defaults GlucoseSimulator_CenterValue 150`).

Loop (`th --app loop flow add-cgm-sim` / `add-pump-sim`): the HUD at the top shows "Add CGM" / "Add Pump"; each opens
an action sheet of that title, where **CGM Simulator** / **Pump Simulator** are set up at once, with no screens (on a
retry the flows go through Settings, the toolbar's last icon, and close it with **Done**). A new CGM Simulator has no
glucose model, so no readings: on the simulator `trio-test-e2e` writes a sine curve (110 ± 20 mg/dL) into its state and
relaunches Loop; by hand, tap the glucose HUD ▸ Simulator Settings ▸ Model (Constant asks for one value).
