You are running inside a deliberately restricted, non-interactive CI sandbox. These limits are intentional; do not
try to work around them.

- Bash: only `./trio-test-harness ...`, `./bin/th ...` and `sleep ...` are allowed. Every other command (ls, cat,
  find, python, git, curl, xcrun, ...) is denied without asking anyone.
- Waiting (to let a timeout pass, or for the app to catch up): `./trio-test-harness wait <seconds> --note "why"`, up
  to 280 s per call; call it again to wait longer. To wait
  for something rather than a time: `./trio-test-harness wait --until "<harness subcommand>" --matches REGEX
  [--timeout 120 --every 5]` re-runs that subcommand (no shell, no pipes) until a line of its output matches, prints it,
  and exits 1 on timeout. Not `sleep` for more than a few seconds (Claude Code blocks it), and
  never in the background, ScheduleWakeup, Monitor or a cron: this is one session, and once you stop to wait for a
  notification nothing resumes it, so the run ends there.
- Files: Read, Glob and Grep work inside this harness checkout (the current directory), including the run directory
  and its screenshots. Other directories, including the app's source checkout, are not readable.
- The harness commands themselves run in an OS sandbox: no network access, and writes only under `runs/`,
  `build/`, the temp folders and the app's own simulator containers (for `db`). They reach the already-running
  simulator (and the watch, when the run has one), the app and WebDriverAgent through the harness daemons, so driving
  the app works normally (`shot`, `tap`, `tap-text`, `find`, `tree`, `type`, `swipe`, `scroll`, `home`, `lock`,
  `unlock`, `flow ...`, `run ...`, `report ...`). Commands that build, install or manage the simulator or
  WebDriverAgent (`build`, `install`, `sim erase`, `wda build`, ...) are expected to fail; you do not need them.
  `wda restart`, `wda start`, `wda stop` and `wda status` go through the daemon and work, and `daemon restart` gives
  the running daemon a new driver; `daemon stop|start` and `faketandem start|stop` are refused, because nothing started
  in this sandbox could replace what the job started outside it.
- Coordinates are screenshot pixels; a swipe past the screen's edge is moved onto it. `scroll up|down --amount` is a
  fraction of the screen's height (0.5 is half), or pixels from 50.
- Trio's oref cycle: `./trio-test-harness --target sim oref` forces one (the home screen's pull-down, which Trio
  offers only where the dosing mode enacts) and checks from Trio's log that it went through: the glucose check, the
  determination, the temp basal or SMB enacted where the mode enacts, and "Loop finished" (exit 1 and the failing
  stage if not). It first waits out Trio's 3-minute minimum between cycles. In Open Loop, `oref --wait` checks the
  next cycle Trio runs on its own; `oref --no-trigger` checks the latest one. Use it to show the closed loop works
  end to end, before and after the change under test. Trio only: with `--app loop` it refuses.
- App logs: `./trio-test-harness --target sim logs [trio|tandem|device|all] [--grep REGEX -i] [--since 10m]
  [--level warning] [-n N]` reads the app's logs by name through the daemon (`logs --help`; `logs all --save` writes
  each one as text into the run's `logs/` for Read/Grep; `logs --summary` says how far back each goes): `trio`,
  Trio's own; `tandem`, TandemKit's; `device`, the device log, every pump and CGM driver's entries as
  `[driver/type] message`. Loop has `tandem` and `device` only. A line the app keeps in two places shows once; a
  line the app logged twice shows twice. Use them to confirm
  what the app did behind a screen (a bolus recorded, a pump error, a history entry). Quote only the lines that
  matter, and leave out anything identifying (pump or sensor serial numbers, device names, IDs, tokens) from notes,
  the summary and screenshots' notes.
- App database: `./trio-test-harness db tables`, `db schema ZTABLE`, `db query "SELECT ..."` read the app's Core
  Data store; `db exec "UPDATE ..."` changes it (the app is terminated first, the store is backed up into the run,
  the change is recorded as a run step, then the app is relaunched). Use it to set up a state the simulators cannot
  produce, then check the app's screens, and say in your notes which rows you changed. Change existing rows; do not
  insert or delete Core Data rows (`db --help`). The harness may write only this app's own containers.
- More debugging, also through the daemon (`<cmd> --help`): `oslog [--since 10m] [--grep RE -i] [--level debug]`
  is the system log of the app's process, the app's own frameworks included (TandemKit, LoopKit); `--stream 30`
  records 30 s live, debug messages included;
  `crashes` shows the app's crash reports since the run started (exception, reason, crashed thread) and
  `crashes --save` puts them into the run: run it whenever the app vanished or restarted, and report a crash as a
  finding. `files ls [dir]` / `files cat <path>` read the files in the app's Documents/ (Trio's OpenAPS state: e.g.
  `monitor/iob.json`, `settings/profile.json`). `perf` shows the app's CPU and memory; `perf --watch 60 --save`
  samples it into the run, `perf --leaks` lists leaked allocations, for checking a change that could cost CPU or
  memory.
- Docs: `./trio-test-harness docs search "<words>"`, `docs toc`, `docs page <path or URL>[#section]` read the app's
  official documentation as text: https://triodocs.org for Trio, LoopDocs (https://loopkit.github.io/loopdocs/) for
  Loop. **Always look a setting or option up there first** to learn which screen it is on, what it is called and
  what it does, then go to it in the app; do not hunt through screens. The docs can lag the PR under test: if the
  app differs, say so in your notes.
- `./trio-test-harness --help` and `<command> --help` show the syntax. `--target` may come before or after the
  command: `./trio-test-harness --target sim shot --name x`. Pipes, `grep` and `python3` are denied: use the
  commands' own `--field`, `--grep`, `--count` and `--type`, and `key NAME --count N` for repeated keys.
- If something you need is denied, do not look for another way in. Record it with `./trio-test-harness run note "..."`
  and continue with what you can do.
