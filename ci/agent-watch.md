A watch simulator paired with the iPhone simulator runs the app's Watch app, started before the phone app so the two
are connected (`pr.md` says why this run includes the watch). Drive it like the phone with `--target watch`:
`./trio-test-harness --target watch shot --name ... --note ...`, `tap-text`, `find`, `tree`, `tap X Y`, `home`, and
`./trio-test-harness --target watch crown 0.5` to turn the Digital Crown (1.0 = one full turn; `--velocity` in turns
a second). A bolus confirmation takes about half a turn in Trio, one full turn at `--velocity 1` in Loop. Read the Apple Watch
section of the trio-ui-drive skill (`.claude/skills/trio-ui-drive/SKILL.md`) first: it lists Trio's and Loop's Watch
screens, how much crown turn confirms a bolus, and where a tap lands.
The job built and started the watch agent (the XCUITest runner behind tap, tree and crown) before you, outside your
sandbox. `./trio-test-harness watch agent status` checks it; `watch agent start|stop|build` and `watch pair` are
refused here, because from inside the sandbox they can only stop it. If a watch command says the agent is not running,
note that and report it rather than trying to restart it.
When the change could look different on another watch setting, also try it there:
- `./trio-test-harness watch settings --wrist left|right --crown left|right --text-size <size>` (sizes: default,
  extra-small … extra-extra-extra-large, accessibility-medium … accessibility-extra-extra-extra-large); the Watch app is
  relaunched to read them, the simulator keeps running. Say in your notes what you changed.
Another watch size is chosen when the run starts (the `watch_model` input), not from inside it.
What the phone did with a Watch request: Trio logs it in its own file (`./trio-test-harness --target sim logs trio --grep
WatchManager`); Loop only to the system log (`./trio-test-harness --target sim oslog --grep WatchDataManager --since 10m`).
Screenshot the watch and the phone at each step that involves both.
