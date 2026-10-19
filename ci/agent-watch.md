A watch simulator paired with the iPhone simulator runs the app's Watch app, started before the phone app so the two
are connected (`pr.md` says why this run includes the watch). Drive it like the phone with `--target watch`:
`./trio-test-harness --target watch shot --name ... --note ...`, `tap-text`, `find`, `tree`, `tap X Y`, `home`, and
`./trio-test-harness --target watch crown 0.5` to turn the Digital Crown (1.0 = one full turn; `--velocity` in turns
a second). A bolus confirmation takes about half a turn in Trio, one full turn at `--velocity 1` in Loop. Read the Apple Watch
section of the trio-ui-drive skill (`.claude/skills/trio-ui-drive/SKILL.md`) first: it lists Trio's and Loop's Watch
screens, how much crown turn confirms a bolus, and where a tap lands.
When the change could look different on another watch, also try it there:
- `./trio-test-harness watch settings --wrist left|right --crown left|right --text-size <size>` (sizes: default,
  extra-small … extra-extra-extra-large, accessibility-medium … accessibility-extra-extra-extra-large); the Watch app is
  relaunched to read them, the simulator keeps running. Say in your notes what you changed.
- `./trio-test-harness watch pair --model 40mm` (or 41mm, 42mm, 44mm, 45mm, 46mm, 49mm) starts another watch size
  (`watch model` lists them; the smallest and largest screens show layout problems first), shutting the current pair
  down. Each model has its own phone simulator: a new one takes about 10 minutes and has no fake CGM or pump until you
  add them (`flow add-cgm-sim`, `flow add-pump-sim`).
What the phone did with a Watch request: Trio logs it in its own file (`./trio-test-harness --target sim logs trio --grep
WatchManager`); Loop only to the system log (`./trio-test-harness --target sim oslog --grep WatchDataManager --since 10m`).
Screenshot the watch and the phone at each step that involves both.
