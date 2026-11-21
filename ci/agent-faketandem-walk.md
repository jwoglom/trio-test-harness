The pump has not been added to the app yet. Walk its setup in the app the way a user would, and take a screenshot of
every screen:
1. Trio: Home ▸ "Add pump" (or Settings ▸ Devices ▸ Insulin Pump) ▸ "Tandem". Loop: Settings ▸ Add Pump ▸ "Tandem".
2. TandemKit's setup asks for the insulin type and the pump's sounds, then moves to pairing. The pairing screen asks
   you to put the Mobi on its charging pad and then double-tap its button. faketandem starts at the double-tap step
   (PairStep1). When the screen asks for the double tap, do what the pump would do:
   `./trio-test-harness faketandem pairing PairStep2`. The app then asks for the pairing code, which is **123456**
   (`faketandem status` shows it too). Type it and confirm.
3. If the app says Control-IQ will be turned off, accept.
4. If a "Load Pump History" step follows pairing, it offers to read the pump's last 6 hours, so the app knows the
   insulin the pump already gave. When `pr.md` names a scenario, the pump has that history. Before you choose, run
   `faketandem status` and `faketandem history` to see what is there. Load it, unless the PR asks for Do Not Load to
   be tested, and screenshot the progress and the result. Then finish the setup.
5. Afterwards, check:
   - `faketandem status` shows the pump connected.
   - The app's home screen shows the pump: its status and reservoir.
   - The app's treatment or insulin history lists the boluses `faketandem history` shows, from before the pairing
     when they were loaded.
   - The app's IOB.
   - When the scenario resets the pump or changes its clock (`out-of-shelf-mode`, `pump-reset`, `reboot-kept-clock`,
     `clock-*`): setup sets the pump's clock to the phone's (a far-off clock must be set before the app doses), every
     pump bolus `faketandem history` shows after the last reset appears once in the app, at the time the scenario gave
     it, nothing from before a reset or shelf mode appears, and nothing is dated in the future. A bolus in the app
     at the wrong time, twice, or from before a reset is a finding: note its amount, the time the app shows and the
     time `faketandem history` gives it.
   Take screenshots of each.
If a step fails, take a screenshot, read `logs tandem --since 10m`, write a note, and try that step once more.
