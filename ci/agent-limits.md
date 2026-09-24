You are running inside a deliberately restricted, non-interactive CI sandbox. These limits are intentional; do not
try to work around them.

- Bash: only `./trio-test-harness ...`, `./bin/th ...` and `sleep ...` are allowed. Every other command (ls, cat,
  find, python, git, curl, xcrun, ...) is denied without asking anyone.
- Files: Read, Glob and Grep work inside this harness checkout (the current directory), including the run directory
  and its screenshots. Other directories, including the Trio source checkout, are not readable.
- The harness commands themselves run in an OS sandbox: no network access, and writes only under `runs/` and
  `build/`. They reach the already-running simulator, Trio and WebDriverAgent through the harness daemon, so driving
  the app works normally (`shot`, `tap`, `tap-text`, `find`, `tree`, `type`, `swipe`, `scroll`, `home`, `lock`,
  `unlock`, `flow ...`, `run ...`, `report ...`). Commands that build, install or manage the simulator or
  WebDriverAgent (`build`, `install`, `sim erase`, `wda build/start`, ...) are expected to fail; you do not need them.
- `./trio-test-harness --help` and `<command> --help` show the syntax. `--target` goes before the command:
  `./trio-test-harness --target sim shot --name x`.
- If something you need is denied, do not look for another way in. Record it with `./trio-test-harness run note "..."`
  and continue with what you can do.
