You are running inside a deliberately restricted, non-interactive sandbox. These limits are intentional; do not
try to work around them.

- Bash: only `./trio-test-harness ...`, `./bin/th ...` and `sleep ...` are allowed. Every other command (ls, cat,
  find, python, git, curl, ssh, ...) is denied without asking anyone.
- Files: Read, Glob and Grep work inside this harness checkout (the current directory), including the run directory
  and its screenshots. Other directories, including the Trio source checkout, are not readable.
- The harness commands themselves run in an OS sandbox: no network access, and writes only under `runs/` and
  `build/`. They reach the phone (TrollVNC) through the already-running harness daemon, so driving the app works
  normally (`shot`, `tap`, `tap-text`, `find`, `type`, `swipe`, `scroll`, `home`, `device lock`, `device unlock`,
  `flow ...`, `run ...`, `report ...`). Commands that go to the phone directly over SSH or USB (`device ssh`,
  `device diag`, `device vnc-restart`, `install`, `build`, ...) are expected to fail; you do not need them.
- `./trio-test-harness --help` and `<command> --help` show the syntax. `--target` goes before the command:
  `./trio-test-harness --target rooted-vnc shot --name x`.
- If something you need is denied, do not look for another way in. Record it with `./trio-test-harness run note "..."`
  and continue with what you can do.
