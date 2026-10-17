You are running inside a deliberately restricted, non-interactive sandbox. These limits are intentional; do not
try to work around them.

- Bash: only `./trio-test-harness ...`, `./bin/th ...` and `sleep ...` are allowed. Every other command (ls, cat,
  find, python, git, curl, ssh, ...) is denied without asking anyone.
- Files: Read, Glob and Grep work inside this harness checkout (the current directory), including the run directory
  and its screenshots. Other directories, including the app's source checkout, are not readable.
- The harness commands themselves run in an OS sandbox: no network access, and writes only under `runs/` and
  `build/`. They reach the phone (TrollVNC) through the already-running harness daemon, so driving the app works
  normally (`shot`, `tap`, `tap-text`, `find`, `type`, `swipe`, `scroll`, `home`, `device lock`, `device unlock`,
  `flow ...`, `run ...`, `report ...`). Commands that go to the phone directly over SSH or USB (`device ssh`,
  `device diag`, `device vnc-restart`, `install`, `build`, ...) are expected to fail; you do not need them.
- App logs: `./trio-test-harness --target rooted-vnc logs [trio|tandem|all] [--grep REGEX -i] [--since 10m] [--level warning]
  [-n N]` reads Trio's and TandemKit's log files from the app through the daemon (`logs --help`; `logs all --save`
  copies them into the run's `logs/` for Read/Grep); Loop writes only TandemKit's (`tandem`). Use them to confirm
  what the app did behind a screen (a bolus recorded, a pump error, a history entry). Quote only the lines that
  matter, and leave out anything identifying (pump or sensor serial numbers, device names, IDs, tokens) from notes,
  the summary and screenshots' notes.
- More debugging, also through the daemon (`<cmd> --help`): `oslog [--since 10m] [--grep RE -i] [--level debug]`
  is the system log of the app's process, the app's own frameworks included (TandemKit, LoopKit);
  `crashes` shows the app's crash reports since the run started (exception, reason, crashed thread) and
  `crashes --save` puts them into the run: run it whenever the app vanished or restarted, and report a crash as a
  finding. `files ls [dir]` / `files cat <path>` read the files in the app's Documents/ (Trio's OpenAPS state: e.g.
  `monitor/iob.json`, `settings/profile.json`). `perf` shows the app's CPU and memory; `perf --watch 60 --save`
  samples it into the run, for checking a change that could cost CPU or memory.
- Docs: `./trio-test-harness docs search "<words>"`, `docs toc`, `docs page <path or URL>[#section]` read the app's
  official documentation as text: https://triodocs.org for Trio, LoopDocs (https://loopkit.github.io/loopdocs/) for
  Loop. **Always look a setting or option up there first** to learn which screen it is on, what it is called and
  what it does, then go to it in the app; do not hunt through screens. The docs can lag the PR under test: if the
  app differs, say so in your notes.
- `./trio-test-harness --help` and `<command> --help` show the syntax. `--target` goes before the command:
  `./trio-test-harness --target rooted-vnc shot --name x`.
- If something you need is denied, do not look for another way in. Record it with `./trio-test-harness run note "..."`
  and continue with what you can do.
