A watch simulator paired with the iPhone simulator runs Trio's Watch app, started before the phone app so the two are
connected. Drive it like the phone with `--target watch`: `./trio-test-harness --target watch shot --name ... --note ...`,
`tap-text`, `find`, `tree`, `tap X Y`, `home`, and `./trio-test-harness --target watch crown 0.5` to turn the Digital
Crown (1.0 = one full turn; about half a turn completes a bolus confirmation). Read the Apple Watch section of the
trio-ui-drive skill (`.claude/skills/trio-ui-drive/SKILL.md`) first: it lists the Watch screens and where a tap lands.
What the phone did with a Watch request is in `./trio-test-harness --target sim logs trio --grep WatchManager`.
Screenshot the watch and the phone at each step that involves both.
